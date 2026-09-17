"""媒体原语的测试 —— 重点是 drawtext 的转义与降级。

这一组测试来自一次**真实的排障**：氛围镜头的标题在 Windows 上完全画不出来，
现象是 ffmpeg 报 ``No option name near '/Windows/Fonts/msyh.ttc:...'``。
根因有两个，都是 ffmpeg 构建相关的坑：

1. ``fontfile`` 需要**两级转义**（滤镜描述一级 + 选项值一级），
   只写 ``\\:`` 会被第一级解析吃掉；
2. 部分 Windows 构建缺 fontconfig 配置，``drawtext`` 直接初始化失败。

因此这里既锁住正确的转义写法，也锁住"文字画不出来时不能拖垮整个镜头"的降级行为。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scidirector_ai.media import (
    MediaToolError,
    _build_filtergraph,
    _escape_drawtext,
    _escape_fontfile,
    extract_frames,
    find_font,
    render_ambient,
    reset_drawtext_cache,
)
from scidirector_ai.sandbox.runner import SandboxRunner

HAS_FFMPEG = shutil.which("ffmpeg") is not None
requires_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="需要 ffmpeg")


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """每个用例前后都重置 drawtext 缓存 —— 它是一个进程级可变状态。"""
    reset_drawtext_cache()
    yield
    reset_drawtext_cache()


# ===========================================================================
# 转义
# ===========================================================================


class TestFontfileEscaping:
    def test_windows_path_gets_two_level_escaping(self) -> None:
        """**核心回归**：Windows 路径的盘符冒号必须写成两个反斜杠 + 冒号。

        只写一个反斜杠时 ffmpeg 会报
        ``No option name near '/Windows/Fonts/msyh.ttc:...'`` ——
        第一级解析把反斜杠吃掉后，第二级仍然在冒号处把选项切开。
        """
        bs = chr(92)
        escaped = _escape_fontfile("C:/Windows/Fonts/msyh.ttc")
        assert escaped == f"C{bs}{bs}:/Windows/Fonts/msyh.ttc"
        assert escaped.count(bs) == 2

    def test_posix_path_is_unchanged(self) -> None:
        """Linux 容器里的路径没有冒号，不应被改动。"""
        posix = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
        assert _escape_fontfile(posix) == posix

    def test_backslashes_in_path_are_doubled_first(self) -> None:
        """先处理反斜杠再处理冒号，否则新加的反斜杠会被再翻一倍。"""
        bs = chr(92)
        escaped = _escape_fontfile("C:\\Windows\\Fonts\\msyh.ttc")
        # 3 个原始反斜杠 -> 6 个；冒号 -> 额外 2 个，共 8 个
        assert escaped.count(bs) == 8


class TestTextEscaping:
    def test_percent_is_not_escaped(self) -> None:
        """``%`` 不需要转义（实测裸 ``100%`` 渲染正常）。

        早期版本把它转成 ``\\%``，结果反斜杠被渲染进了画面。
        """
        assert _escape_drawtext("正确率 100%") == "正确率 100%"

    def test_chinese_and_fullwidth_punctuation_pass_through(self) -> None:
        text = "第 1 讲：勾股定理（面积法）"
        assert _escape_drawtext(text) == text

    def test_backslash_is_doubled(self) -> None:
        assert _escape_drawtext("a\\b") == "a\\\\b"

    def test_single_quote_is_replaced(self) -> None:
        """单引号是包裹符，替换成同形全角字符，避免破坏引号配对。"""
        escaped = _escape_drawtext("it's")
        assert "'" not in escaped
        assert "\u2019" in escaped

    @pytest.mark.parametrize("raw", ["a\nb", "a\r\nb", "a\rb"])
    def test_newlines_become_spaces(self, raw: str) -> None:
        """换行会破坏单行滤镜语法，统一压成空格。"""
        escaped = _escape_drawtext(raw)
        assert "\n" not in escaped and "\r" not in escaped
        assert escaped == "a b"


class TestFiltergraphComposition:
    def test_without_text(self) -> None:
        vf = _build_filtergraph(duration_sec=4.0, width=1920, height=1080, fps=30,
                                colors=("0x0B1020", "0x4F8CFF", "0xFF6B6B"))
        assert vf.startswith("gradients=")
        assert "drawtext" not in vf

    def test_with_text_includes_fontfile_and_alpha(self) -> None:
        vf = _build_filtergraph(
            duration_sec=4.0, width=1920, height=1080, fps=30,
            colors=("0x0B1020", "0x4F8CFF", "0xFF6B6B"),
            font="C:/Windows/Fonts/msyh.ttc", text="标题",
        )
        assert "drawtext=" in vf
        assert "fontfile=C" + chr(92) * 2 + ":" in vf, "fontfile 未做两级转义"
        assert "text='标题'" in vf
        assert "alpha='if(lt(t,0.8)" in vf

    def test_text_is_ignored_without_font(self) -> None:
        """没有字体时不该把 drawtext 拼进滤镜图（那必然失败）。"""
        vf = _build_filtergraph(
            duration_sec=4.0, width=320, height=240, fps=10,
            colors=("0x0B1020", "0x4F8CFF", "0xFF6B6B"), font=None, text="标题",
        )
        assert "drawtext" not in vf


# ===========================================================================
# 真实渲染
# ===========================================================================


@requires_ffmpeg
class TestRenderAmbient:
    def test_renders_without_text(self, tmp_path: Path) -> None:
        out = tmp_path / "plain.mp4"
        path = render_ambient(
            out, SandboxRunner(), duration_sec=1.0, width=320, height=240, fps=10
        )
        assert Path(path).is_file() and Path(path).stat().st_size > 0

    def test_renders_with_chinese_title_containing_specials(self, tmp_path: Path) -> None:
        """**端到端回归**：带中文、全角括号、百分号、ASCII 冒号的标题必须能出片。

        这条用例如果失败，说明 drawtext 的转义又写错了 ——
        而那种错误只在"标题里恰好含某个字符"时才暴露，极难靠人工发现。
        """
        if not find_font():
            pytest.skip("本机没有可用的中文字体")

        out = tmp_path / "titled.mp4"
        path = render_ambient(
            out,
            SandboxRunner(),
            duration_sec=1.0,
            width=320,
            height=240,
            fps=10,
            text="第 1 讲：勾股定理（100% 正确）a:b",
        )
        assert Path(path).is_file() and Path(path).stat().st_size > 0

    def test_falls_back_to_no_text_when_drawtext_fails(self, tmp_path: Path) -> None:
        """drawtext 不可用时**必须**去掉文字重试，而不是让镜头渲染失败。

        标题只是装饰；因为它画不出来就丢掉整个镜头，是把环境问题
        升级成了内容生产事故。
        """
        if not find_font():
            pytest.skip("本机没有可用的中文字体")

        from scidirector_ai import media as media_module

        # 模拟"带文字渲染必然失败"：让 drawtext 分支总是拿到失败结果。
        original_build = media_module._build_filtergraph

        def broken(**kwargs: object) -> str:
            return original_build(**kwargs)  # 图本身没问题

        real_run = media_module.SandboxRunner.run
        calls: list[str] = []

        def run_with_broken_drawtext(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal calls
            vf = argv[argv.index("-i") + 1] if "-i" in argv else ""
            calls.append(vf)
            if "drawtext" in vf:
                # 返回一个失败结果，模拟该构建不支持 drawtext。
                from scidirector_ai.sandbox.runner import ExecResult

                return ExecResult(command=list(argv), returncode=-22, stderr="drawtext init failed")
            return real_run(self, argv, **kwargs)

        import unittest.mock as mock

        with mock.patch.object(media_module.SandboxRunner, "run", run_with_broken_drawtext):
            out = tmp_path / "fallback.mp4"
            path = render_ambient(
                out, SandboxRunner(), duration_sec=1.0, width=320, height=240,
                fps=10, text="标题",
            )

        assert Path(path).is_file(), "降级后仍未产出视频"
        assert len(calls) == 2, f"应当先试带文字、再去掉文字重试，实际调用 {len(calls)} 次"
        assert "drawtext" in calls[0], "第一次应当带文字"
        assert "drawtext" not in calls[1], "第二次应当去掉文字"

    def test_second_call_skips_text_after_failure(self, tmp_path: Path) -> None:
        """一旦确认 drawtext 不可用，后续镜头应直接跳过文字，不再白跑一次。"""
        if not find_font():
            pytest.skip("本机没有可用的中文字体")

        from scidirector_ai import media as media_module
        from scidirector_ai.sandbox.runner import ExecResult

        real_run = media_module.SandboxRunner.run
        calls: list[str] = []

        def run_with_broken_drawtext(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            vf = argv[argv.index("-i") + 1] if "-i" in argv else ""
            calls.append(vf)
            if "drawtext" in vf:
                return ExecResult(command=list(argv), returncode=-22, stderr="drawtext init failed")
            return real_run(self, argv, **kwargs)

        import unittest.mock as mock

        with mock.patch.object(media_module.SandboxRunner, "run", run_with_broken_drawtext):
            render_ambient(tmp_path / "a.mp4", SandboxRunner(), duration_sec=1.0,
                           width=320, height=240, fps=10, text="标题一")
            first_round = len(calls)
            render_ambient(tmp_path / "b.mp4", SandboxRunner(), duration_sec=1.0,
                           width=320, height=240, fps=10, text="标题二")

        # 第二轮只应有 1 次调用（直接用无文字版本），而不是又试一次带文字的。
        assert len(calls) - first_round == 1, (
            f"第二轮调用了 {len(calls) - first_round} 次，说明没有复用'不可用'的结论"
        )

    def test_invalid_duration_still_produces_output(self, tmp_path: Path) -> None:
        """极短时长也要能出片（分镜时长可能被导演压到 2 秒以下）。"""
        out = tmp_path / "short.mp4"
        path = render_ambient(out, SandboxRunner(), duration_sec=0.3,
                              width=320, height=240, fps=10)
        assert Path(path).is_file()

    def test_reports_error_when_ffmpeg_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(MediaToolError) as exc_info:
            render_ambient(tmp_path / "x.mp4", SandboxRunner(), duration_sec=1.0,
                           width=320, height=240, fps=10)
        assert "ffmpeg" in str(exc_info.value)


# ===========================================================================
# 抽帧 —— VLM 审查的唯一输入
# ===========================================================================


@requires_ffmpeg
class TestExtractFrames:
    """抽帧是 Critic 的眼睛，而这条链路失败时**不会报错**。

    它只会让审查降级为「转人工」，现象是「每个镜头都需要人工确认」——
    看起来像模型能力问题，实际是媒体层在静默失败。
    所以这里的断言必须是「真的抽出了帧」，而不是某个中间量。
    """

    def test_extracts_frames_under_sandbox_memory_limit(self, tmp_path: Path) -> None:
        """**回归**：沙盒默认内存上限下，抽帧必须真的产出帧。

        抽帧命令带 ``-vf scale``，ffmpeg 为此要预留大量**虚拟地址空间**
        （本机实测：RSS 仅 56MB，地址空间却要约 2GB）。把 ``max_memory_mb``
        直接当成 RLIMIT_AS 用时，正常进程会在远未触及内存上限时被杀，
        而 ffmpeg 的**退出码仍然是 0、产物却是空的** ——
        于是抽帧「全部失败」，Critic 对每一个镜头都降级，
        整条链路里没有任何一处会报错。
        """
        video = render_ambient(
            tmp_path / "clip.mp4",
            SandboxRunner(),
            duration_sec=1.0,
            width=320,
            height=240,
            fps=10,
        )
        frames = extract_frames(video, tmp_path / "frames", SandboxRunner(), count=3)

        # count=3 会取 3 个等间隔中点 + 首帧 + 末帧 = 5 帧。
        # 断言 >= 4 而不是 == 5：个别取样点上 ffmpeg 可能取不到帧，
        # 但只要还能拿到足够多的帧，审查就不该降级。
        assert len(frames) >= 4, (
            f"只抽出 {len(frames)} 帧，Critic 会因缺帧降级 —— "
            f"先确认沙盒的 RLIMIT 有没有把正常 ffmpeg 误杀"
        )
        for frame in frames:
            path = Path(frame)
            assert path.is_file(), f"返回了不存在的帧路径: {frame}"
            assert path.stat().st_size > 0, f"抽出了空帧: {frame}"
