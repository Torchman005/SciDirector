"""``ManimSandbox`` 的集成测试 —— 用**假的 manim 模块**跑真实子进程。

为什么不用 mock 掉 subprocess：
本模块的全部价值就在于"真的起了子进程、真的被杀了、真的没留下孤儿"。
把这些换成 mock，测试就变成了对自身实现的同义反复，一点安全感都换不来。

做法：在 tmp 目录下造一个名为 ``manim`` 的包，通过 ``env_extra`` 注入
``PYTHONPATH``，于是 ``python -m manim ...`` 会执行我们的假实现。
它支持四种行为（正常出片 / 死循环 / 内存爆炸 / 崩溃），
从而可以**确定性地**覆盖超时、内存、错误分类等关键分支 —— 而且不需要真的装 Manim。

真实 Manim 与假实现的接口完全一致（命令行参数、媒体目录结构），
因此这里覆盖的进程管理、超时、产物校验逻辑对生产路径同样有效。
"""

from __future__ import annotations

import sys
import textwrap
import time
from pathlib import Path

import pytest

from scidirector_ai.config import Settings
from scidirector_ai.sandbox.manim import (
    DEFAULT_SCENE_CLASS,
    ManimRenderRequest,
    ManimSandbox,
    ManimSandboxError,
    extract_scene_class,
    newest_mp4,
)

# --- 假 manim 包 -----------------------------------------------------------
# 命令行接口与真实 Manim 保持一致：`python -m manim -ql --format=mp4 --media_dir D script.py Class`
_FAKE_INIT = '__version__ = "0.0.0-fake"\n'

_FAKE_MAIN = '''
import os
import pathlib
import subprocess
import sys


def main() -> None:
    args = sys.argv[1:]
    media_dir = None
    quality = "l"
    positional = []

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--media_dir":
            media_dir = args[i + 1]
            i += 2
            continue
        if arg.startswith("-q"):
            quality = arg[2:] or "l"
            i += 1
            continue
        if arg.startswith("--"):
            i += 1
            continue
        positional.append(arg)
        i += 1

    behavior = os.environ.get("FAKE_MANIM_BEHAVIOR", "ok")

    if behavior == "loop":
        # 模拟死循环：永不返回，只能靠超时杀掉。
        while True:
            pass

    if behavior == "memory":
        # 模拟内存爆炸：每次 8MB，直到被内存限制拦住。
        chunks = []
        while True:
            chunks.append(bytearray(8 * 1024 * 1024))

    if behavior == "crash":
        sys.stderr.write("FakeManim: Latex error: File `amsmath.sty' not found\\n")
        sys.stderr.write("FakeManim: 渲染在第 42 行失败\\n")
        sys.exit(3)

    if behavior == "nooutput":
        # 报成功但不产出文件（例如场景类名写错）。
        print("FakeManim: done (no output produced)")
        sys.exit(0)

    script = positional[0]
    scene = positional[1]
    out = pathlib.Path(media_dir) / "videos" / pathlib.Path(script).stem / quality / (scene + ".mp4")
    out.parent.mkdir(parents=True, exist_ok=True)

    if behavior == "corrupt":
        # 产出非空但根本不是有效视频的文件，用于验证"产物校验"这一层。
        out.write_bytes(b"this is not an mp4 at all" * 64)
        print("FakeManim: wrote a corrupt file")
        sys.exit(0)

    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y",
         "-f", "lavfi", "-i", "color=c=0x0B1020:s=320x240:d=1:r=10",
         "-pix_fmt", "yuv420p", str(out)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print("FakeManim: rendered", out)


main()
'''

_VALID_CODE = textwrap.dedent(
    """
    from manim import *


    class SciShotScene(Scene):
        def construct(self):
            self.play(Write(Text("测试场景", font_size=48)))
    """
).strip()


@pytest.fixture(scope="module")
def fake_manim_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """造出一个假 manim 包，返回其 PYTHONPATH 根目录。"""
    root = tmp_path_factory.mktemp("fakemanim")
    pkg = root / "manim"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(_FAKE_INIT, encoding="utf-8")
    (pkg / "__main__.py").write_text(_FAKE_MAIN, encoding="utf-8")
    return root


def make_sandbox(work_dir: Path, **overrides: object) -> ManimSandbox:
    """构造一个指向假 manim 的沙盒。"""
    settings = Settings(
        env="test",
        # 用当前解释器，避免测试机 PATH 里没有 `python` 的情况。
        sandbox_python_bin=sys.executable,
        sandbox_work_dir=str(work_dir),
        **overrides,  # type: ignore[arg-type]
    )
    return ManimSandbox(settings)


def make_request(work_dir: Path, fake_root: Path, behavior: str, code: str = _VALID_CODE) -> ManimRenderRequest:
    return ManimRenderRequest(
        code=code,
        output_dir=work_dir,
        expected_duration_sec=2.0,
        env_extra={"PYTHONPATH": str(fake_root), "FAKE_MANIM_BEHAVIOR": behavior},
    )


# ===========================================================================
# 正常路径
# ===========================================================================


class TestSuccessfulRender:
    def test_renders_and_validates_output(self, tmp_path: Path, fake_manim_root: Path) -> None:
        sandbox = make_sandbox(tmp_path / "work", manim_timeout_sec=60)
        result = sandbox.render(make_request(tmp_path / "work", fake_manim_root, "ok"))

        assert Path(result.video_path).is_file()
        assert result.scene_class == "SciShotScene"
        # 第 3 层防护：产物必须能被 ffprobe 解析且参数有效。
        assert result.media.valid
        assert result.media.width == 320
        assert result.media.duration_sec > 0
        assert result.timeout_sec == 60
        assert "FakeManim: rendered" in result.stdout_tail

    def test_clears_previous_output(self, tmp_path: Path, fake_manim_root: Path) -> None:
        """上一轮的产物必须被清掉。

        不清会怎样：本轮渲染失败但目录里还留着上轮的 mp4，
        递归搜索会命中旧文件，把**失败误判成成功** —— 这是最隐蔽的一类 bug。
        """
        work = tmp_path / "work"
        stale = work / "media" / "videos" / "scene" / "480p15" / "Stale.mp4"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"stale")

        sandbox = make_sandbox(work, manim_timeout_sec=60)
        result = sandbox.render(make_request(work, fake_manim_root, "ok"))

        assert Path(result.video_path).name != "Stale.mp4"
        assert not stale.exists()


# ===========================================================================
# 超时：需求的核心
# ===========================================================================


class TestTimeout:
    def test_dead_loop_is_killed_and_reported(self, tmp_path: Path, fake_manim_root: Path) -> None:
        """死循环的 Manim 渲染必须在 30s 级别的超时点被杀死。

        这里把超时设成 5s 以保持测试快速；生产默认值是 30s
        （见 config.manim_timeout_sec），走的是**同一条**代码路径。
        """
        work = tmp_path / "work"
        sandbox = make_sandbox(work, manim_timeout_sec=5)

        started = time.monotonic()
        with pytest.raises(ManimSandboxError) as exc_info:
            sandbox.render(make_request(work, fake_manim_root, "loop"))
        elapsed = time.monotonic() - started

        err = exc_info.value
        assert err.killed_reason == "timeout"
        assert err.retryable is True, "超时应当可重试（改写代码可能就能修好）"
        assert "超时" in str(err)
        assert elapsed < 20, f"超时后 {elapsed:.1f}s 才返回，终止不及时"
        # 反馈里要带上可调参数，否则运维只知道"超时了"却不知道从哪调。
        assert "SCID_MANIM_TIMEOUT_SEC" in str(err)

    def test_timeout_sec_comes_from_settings(self, tmp_path: Path) -> None:
        assert make_sandbox(tmp_path, manim_timeout_sec=30).timeout_sec == 30
        assert make_sandbox(tmp_path, manim_timeout_sec=120).timeout_sec == 120
        # 默认值就是需求里的 30 秒。
        assert ManimSandbox(Settings(env="test")).timeout_sec == 30


# ===========================================================================
# 内存限制
# ===========================================================================


class TestMemoryLimit:
    def test_memory_hog_is_stopped(self, tmp_path: Path, fake_manim_root: Path) -> None:
        """内存爆炸必须被拦住，且要在超时之前就结束。

        仅靠超时挡不住它：几秒钟就足以把机器打爆。
        因此这条断言同时验证了"内存限制存在"与"它比超时更早生效"。
        """
        work = tmp_path / "work"
        sandbox = make_sandbox(work, manim_timeout_sec=30, manim_max_memory_mb=256)
        assert sandbox.max_memory_mb == 256

        started = time.monotonic()
        with pytest.raises(ManimSandboxError) as exc_info:
            sandbox.render(make_request(work, fake_manim_root, "memory"))
        elapsed = time.monotonic() - started

        err = exc_info.value
        assert err.retryable is True
        # 关键断言：**两种内存限制机制必须归一化成同一种可观测结果**。
        # 监控线程路径是"被杀"，内核限制路径是"分配失败后自行退出"；
        # 如果不归一化，上层就得为同一件事写两套判断（而且很容易漏一套）。
        assert err.killed_reason == "memory", (
            f"内存超限未被识别（killed_reason={err.killed_reason!r}）：{err}"
        )
        # 必须是被资源限制拦住，而不是跑满 30s 超时 ——
        # 否则说明内存限制根本没生效，只是超时"顺便"救了场。
        assert elapsed < 25, f"内存限制未生效，靠超时才结束（{elapsed:.1f}s）"


# ===========================================================================
# 失败分类
# ===========================================================================


class TestFailureClassification:
    def test_compile_error_is_retryable(self, tmp_path: Path, fake_manim_root: Path) -> None:
        """代码/LaTeX 报错是可重试的：把 stderr 回灌给编码智能体，有可能改对。"""
        work = tmp_path / "work"
        sandbox = make_sandbox(work, manim_timeout_sec=30)

        with pytest.raises(ManimSandboxError) as exc_info:
            sandbox.render(make_request(work, fake_manim_root, "crash"))

        err = exc_info.value
        assert err.retryable is True
        assert "退出码 3" in str(err)
        # 细节必须带出来 —— 这是回灌给模型的唯一线索。
        assert "amsmath.sty" in err.detail

    def test_missing_manim_is_not_retryable(self, tmp_path: Path) -> None:
        """缺依赖属于**环境问题**：重试多少次都一样，必须转人工。

        判错方向的代价很不对称：把环境问题判成可重试会无限烧钱。
        """
        work = tmp_path / "work"
        sandbox = make_sandbox(work, manim_timeout_sec=30)
        # 故意不注入假 manim，于是 `python -m manim` 会报 No module named manim。
        request = ManimRenderRequest(code=_VALID_CODE, output_dir=work)

        with pytest.raises(ManimSandboxError) as exc_info:
            sandbox.render(request)

        assert exc_info.value.retryable is False, "缺依赖被误判为可重试"

    def test_success_without_output_is_detected(
        self, tmp_path: Path, fake_manim_root: Path
    ) -> None:
        """「报成功但没产物」必须被判为失败。

        典型成因是场景类名写错。若放行，会产生一个"成功但空"的镜头，
        一路流到合成阶段才炸 —— 那时已经烧掉了整轮渲染。
        """
        work = tmp_path / "work"
        sandbox = make_sandbox(work, manim_timeout_sec=30)

        with pytest.raises(ManimSandboxError) as exc_info:
            sandbox.render(make_request(work, fake_manim_root, "nooutput"))

        assert "未找到输出文件" in str(exc_info.value)
        assert exc_info.value.retryable is True

    def test_corrupt_output_is_rejected(self, tmp_path: Path, fake_manim_root: Path) -> None:
        """产物存在但无法解析 -> 第 3 层校验拦下。

        不拦会怎样：一个坏 mp4 被当成"渲染成功"进入 VLM 审查，
        白烧一次多模态调用，最后在合成阶段才炸。
        """
        work = tmp_path / "work"
        sandbox = make_sandbox(work, manim_timeout_sec=30)

        with pytest.raises(ManimSandboxError) as exc_info:
            sandbox.render(make_request(work, fake_manim_root, "corrupt"))

        err = exc_info.value
        assert err.retryable is True
        assert "产物" in str(err) or "无法解析" in str(err)


# ===========================================================================
# 静态检查（第 0 层）：必须在起进程之前拦住
# ===========================================================================


class TestStaticPolicyGate:
    @pytest.mark.parametrize(
        "dangerous",
        [
            "import os\nos.system('rm -rf /')",
            "import subprocess\nsubprocess.run(['ls'])",
            "eval('1+1')",
            "x = object.__subclasses__()",
            "while True:\n    pass\nimport socket",
        ],
    )
    def test_dangerous_code_is_refused_before_execution(
        self, tmp_path: Path, fake_manim_root: Path, dangerous: str
    ) -> None:
        work = tmp_path / "work"
        sandbox = make_sandbox(work, manim_timeout_sec=30)

        with pytest.raises(ManimSandboxError) as exc_info:
            sandbox.render(ManimRenderRequest(code=dangerous, output_dir=work))

        err = exc_info.value
        assert err.policy is not None, "应当带上静态检查报告，供回灌给编码智能体"
        assert err.policy.has_errors
        # 关键：脚本根本没有被写到磁盘 —— 说明进程从未启动。
        assert not (work / "scene.py").exists(), "危险代码被写盘了，说明拦截发生得太晚"

    def test_feedback_mentions_the_violation(
        self, tmp_path: Path, fake_manim_root: Path
    ) -> None:
        """反馈必须写出**具体问题**，否则模型只能盲改。"""
        work = tmp_path / "work"
        sandbox = make_sandbox(work, manim_timeout_sec=30)
        with pytest.raises(ManimSandboxError) as exc_info:
            sandbox.render(ManimRenderRequest(code="import os\n", output_dir=work))

        feedback = exc_info.value.feedback()
        assert "静态检查未通过" in feedback
        assert "os" in feedback

    def test_syntax_error_is_reported_as_policy_violation(
        self, tmp_path: Path, fake_manim_root: Path
    ) -> None:
        work = tmp_path / "work"
        sandbox = make_sandbox(work, manim_timeout_sec=30)
        with pytest.raises(ManimSandboxError) as exc_info:
            sandbox.render(ManimRenderRequest(code="def broken(:\n", output_dir=work))
        assert "语法错误" in str(exc_info.value.detail) or "语法错误" in exc_info.value.feedback()


# ===========================================================================
# 辅助函数
# ===========================================================================


class TestSceneClassExtraction:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("class MyScene(Scene):\n    pass", "MyScene"),
            ("class Foo(manim.Scene):\n    pass", "Foo"),
            ("from manim import *\n\nclass SciShotScene(Scene):\n    def construct(self): pass", "SciShotScene"),
            # 带基类参数与空格
            ("class  Bar ( ThreeDScene , Scene ) :\n    pass", "Bar"),
        ],
    )
    def test_extracts_class_name(self, code: str, expected: str) -> None:
        assert extract_scene_class(code) == expected

    def test_falls_back_to_convention(self) -> None:
        """找不到类定义时回退到约定类名，而不是抛异常。

        让 Manim 报"找不到这个类"（错误信息明确、可回灌），
        比在沙盒里抛异常更好：后者会绕过统一的失败分类逻辑。
        """
        assert extract_scene_class("x = 1\n") == DEFAULT_SCENE_CLASS


class TestNewestMp4:
    def test_ignores_empty_files(self, tmp_path: Path) -> None:
        """0 字节的 mp4 不算产物。

        ffmpeg 被杀时常常会留下一个空文件 —— 把它当成功会酿成"成功但空白"的镜头。
        """
        (tmp_path / "empty.mp4").write_bytes(b"")
        assert newest_mp4(tmp_path) is None

        good = tmp_path / "sub" / "good.mp4"
        good.parent.mkdir()
        good.write_bytes(b"x" * 128)
        assert newest_mp4(tmp_path) == good

    def test_returns_none_for_missing_dir(self, tmp_path: Path) -> None:
        assert newest_mp4(tmp_path / "nope") is None

    def test_picks_most_recent(self, tmp_path: Path) -> None:
        old = tmp_path / "old.mp4"
        old.write_bytes(b"x" * 64)
        new = tmp_path / "new.mp4"
        new.write_bytes(b"y" * 64)
        # 显式设置 mtime，避免依赖文件系统时间戳精度。
        import os

        os.utime(old, (1_000_000, 1_000_000))
        os.utime(new, (2_000_000, 2_000_000))
        assert newest_mp4(tmp_path) == new
