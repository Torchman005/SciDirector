"""渲染调度层的测试。

分两类：

* **真实出片**：氛围镜头走真实 ffmpeg，验证"渲染成功"这条路径确实能产出
  可被 ffprobe 解析的视频（ffmpeg 在开发机与镜像里都是硬依赖）；
* **契约与分类**：HTML 渲染契约的静态检查、失败分类（可重试 / 不可重试）、
  工厂与可用性探测。

刻意不 mock ffmpeg：这一层的价值就是"真的生成了一个能播的文件"，
mock 掉之后测试只能证明参数拼对了，证明不了产物有效。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scidirector_ai.config import Settings
from scidirector_ai.sandbox.manim import ManimRenderResult, ManimSandboxError
from scidirector_ai.sandbox.runner import SandboxRunner
from scidirector_ai.media import MediaInfo
from scidirector_ai.renderer import (
    ENGINE_BY_TAG,
    HTML_ENGINES,
    LLM_ENGINES,
    AmbientRenderer,
    HtmlRenderer,
    ManimRenderer,
    RendererError,
    RenderRequest,
    build_renderer,
    check_html_contract,
    renderer_availability,
)
# 探测的实现在 config 里（唯一一份）：健康检查与渲染自检共用同一份结论。
from scidirector_ai import config as config_module  # noqa: E402
from scidirector_ai.config import (  # noqa: E402
    _probe_browser_with,
    browser_ready,
    reset_browser_probe_cache,
)

HAS_FFMPEG = shutil.which("ffmpeg") is not None
requires_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="需要 ffmpeg")


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        env="test",
        llm_provider="mock",
        sandbox_work_dir=str(tmp_path / "work"),
        render_width=320,
        render_height=240,
        render_fps=10,
        manim_timeout_sec=30,
        **overrides,  # type: ignore[arg-type]
    )


def make_request(tmp_path: Path, **overrides: object) -> RenderRequest:
    base: dict[str, object] = {
        "shot_id": "job-x-s000",
        "code": "",
        "output_dir": tmp_path / "out",
        "duration_sec": 1.0,
        "width": 320,
        "height": 240,
        "fps": 10,
    }
    base.update(overrides)
    return RenderRequest(**base)  # type: ignore[arg-type]


# ===========================================================================
# 工厂与可用性
# ===========================================================================


class TestFactory:
    @pytest.mark.parametrize(
        ("engine", "expected"),
        [
            ("manim", ManimRenderer),
            ("d3", HtmlRenderer),
            ("echarts", HtmlRenderer),
            ("code_anim", HtmlRenderer),
            ("stock", AmbientRenderer),
        ],
    )
    def test_builds_expected_renderer(
        self, tmp_path: Path, engine: str, expected: type
    ) -> None:
        renderer = build_renderer(engine, make_settings(tmp_path))
        assert isinstance(renderer, expected)
        assert renderer.engine == engine

    def test_unknown_engine_is_a_clear_non_retryable_error(self, tmp_path: Path) -> None:
        """未知引擎必须明确报错，而不是静默降级成某个占位实现。

        静默降级会产出"看起来成功但内容完全不对"的视频 —— 比失败危险得多。
        """
        with pytest.raises(RendererError) as exc_info:
            build_renderer("hologram", make_settings(tmp_path))
        assert exc_info.value.retryable is False
        assert "hologram" in str(exc_info.value)

    def test_availability_report_covers_every_engine(self, tmp_path: Path) -> None:
        report = renderer_availability(make_settings(tmp_path))
        assert set(report) == {"manim", "d3", "echarts", "code_anim", "stock"}
        assert all(isinstance(v, bool) for v in report.values())

    def test_engine_by_tag_matches_go_side_contract(self) -> None:
        """标签 -> 引擎的映射必须与 Go 侧 domain.EngineForTag 一致。

        两侧不一致会导致"Go 认为是 manim、Python 按 d3 渲染"这类
        跨语言契约漂移，且不会在编译期暴露。
        """
        assert ENGINE_BY_TAG == {
            "MATH": "manim",
            "DATA": "d3",
            "CODE": "code_anim",
            "AMBIENCE": "stock",
        }

    def test_engine_groups_are_consistent(self) -> None:
        """引擎分组之间要保持自洽。

        注意 ``echarts`` 已在渲染器里注册，但**没有**任何标签映射到它 ——
        它是 DATA 标签的可选引擎（未来可由风格或配置选用），
        因此断言是"标签映射是引擎集合的子集"，而不是相等。
        """
        assert set(ENGINE_BY_TAG.values()) <= LLM_ENGINES | {"stock"}
        assert not (LLM_ENGINES & {"stock"}), "stock 由 ffmpeg 程序化生成，不该走 LLM"
        assert HTML_ENGINES <= LLM_ENGINES
        assert "manim" not in HTML_ENGINES
        assert "echarts" in LLM_ENGINES, "echarts 是 DATA 的可选引擎，必须保持注册"

    def test_every_registered_engine_is_buildable(self, tmp_path: Path) -> None:
        """每个登记在册的引擎都必须能构造出渲染器。

        防止有人加了映射却忘了加实现，导致任务跑到那一步才炸。
        """
        from scidirector_ai.renderer import _RENDERERS

        settings = make_settings(tmp_path)
        for engine in _RENDERERS:
            renderer = build_renderer(engine, settings)
            assert renderer.engine == engine
            ok, reason = renderer.available()
            assert isinstance(ok, bool)
            if not ok:
                assert reason, f"{engine} 报告不可用却没有给出原因"


# ===========================================================================
# 氛围镜头：真实出片
# ===========================================================================


@requires_ffmpeg
class TestAmbientRenderer:
    def test_produces_a_valid_video(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        renderer = AmbientRenderer(settings)
        runner = SandboxRunner()

        ok, reason = renderer.available()
        assert ok, reason

        result = renderer.render(make_request(tmp_path, duration_sec=1.0), runner)

        assert Path(result.video_path).is_file()
        assert result.media.valid, "产物不是有效视频"
        assert result.width == 320
        assert result.height == 240
        # 允许编码器有几帧的偏差。
        assert 0.5 <= result.duration_sec <= 2.0
        assert result.engine == "stock"

    def test_renders_with_overlay_text(self, tmp_path: Path) -> None:
        """带标题的氛围镜头也要能出片。

        drawtext 的 filter 语法很容易因为未转义的 ``:`` / ``'`` / ``%`` 而失败，
        因此这条路必须有用例覆盖。
        """
        settings = make_settings(tmp_path)
        result = AmbientRenderer(settings).render(
            make_request(
                tmp_path,
                duration_sec=1.0,
                overlay_text="第 1 讲：勾股定理（100% 正确）",
            ),
            SandboxRunner(),
        )
        assert Path(result.video_path).is_file()
        assert result.media.valid

    def test_reports_unavailable_without_ffmpeg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """缺 ffmpeg 时如实报告不可用，且渲染失败是**不可重试**的。"""
        monkeypatch.setattr(shutil, "which", lambda name: None)
        renderer = AmbientRenderer(make_settings(tmp_path))
        ok, reason = renderer.available()
        assert ok is False
        assert "ffmpeg" in reason

        with pytest.raises(RendererError) as exc_info:
            renderer.render(make_request(tmp_path), SandboxRunner())
        assert exc_info.value.retryable is False


# ===========================================================================
# Manim 渲染器：委托给沙盒
# ===========================================================================


class _StubSandbox:
    """沙盒桩：只用于验证"异常映射"这一层，不涉及真实进程。"""

    def __init__(self, *, error: Exception | None = None, ok: bool = True) -> None:
        self.error = error
        self.ok = ok
        self.requests: list[object] = []

    def check_environment(self) -> tuple[bool, str]:
        return (True, "") if self.ok else (False, "桩：环境不可用")

    def render(self, request: object) -> ManimRenderResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ManimRenderResult(
            video_path="/tmp/fake.mp4",
            scene_class="SciShotScene",
            media=MediaInfo(path="/tmp/fake.mp4", duration_sec=2.0, width=320, height=240, fps=10),
            render_cost_sec=1.5,
            stdout_tail="ok",
        )


class TestManimRendererDelegation:
    def test_maps_retryable_failure(self, tmp_path: Path) -> None:
        """沙盒的可重试失败要原样传递（改写代码可能修好）。"""
        sandbox = _StubSandbox(error=ManimSandboxError("渲染失败", retryable=True, detail="LaTeX 报错"))
        renderer = ManimRenderer(make_settings(tmp_path), sandbox)  # type: ignore[arg-type]

        with pytest.raises(RendererError) as exc_info:
            renderer.render(make_request(tmp_path), SandboxRunner())
        assert exc_info.value.retryable is True
        assert "LaTeX 报错" in exc_info.value.detail

    def test_maps_non_retryable_failure(self, tmp_path: Path) -> None:
        """缺依赖这类环境问题必须保持不可重试 —— 否则会无限烧钱重试。"""
        sandbox = _StubSandbox(error=ManimSandboxError("缺 manim", retryable=False))
        renderer = ManimRenderer(make_settings(tmp_path), sandbox)  # type: ignore[arg-type]

        with pytest.raises(RendererError) as exc_info:
            renderer.render(make_request(tmp_path), SandboxRunner())
        assert exc_info.value.retryable is False

    def test_successful_render_is_passed_through(self, tmp_path: Path) -> None:
        sandbox = _StubSandbox()
        renderer = ManimRenderer(make_settings(tmp_path), sandbox)  # type: ignore[arg-type]
        result = renderer.render(make_request(tmp_path, code="class X(Scene): pass"), SandboxRunner())

        assert result.video_path == "/tmp/fake.mp4"
        assert result.engine == "manim"
        assert result.duration_sec == 2.0
        assert len(sandbox.requests) == 1

    def test_available_delegates_to_sandbox(self, tmp_path: Path) -> None:
        assert ManimRenderer(make_settings(tmp_path), _StubSandbox()).available() == (True, "")
        ok, reason = ManimRenderer(make_settings(tmp_path), _StubSandbox(ok=False)).available()
        assert ok is False and "桩" in reason


# ===========================================================================
# HTML 渲染契约
# ===========================================================================


class TestHtmlContract:
    def test_accepts_code_with_seek(self) -> None:
        report = check_html_contract("window.__seek = (t) => {}; window.__ready = true;")
        assert report.ok

    def test_rejects_missing_seek(self) -> None:
        """缺少 __seek 会导致渲染"成功"但画面静止 —— 失败是静默的，必须静态拦住。"""
        report = check_html_contract("<div>只有静态内容</div>")
        assert not report.ok
        assert any("window.__seek" in v.reason for v in report.errors)

    def test_missing_seek_feedback_is_readable(self) -> None:
        """违规反馈会被**原样回灌给编码智能体**，必须是通顺且可执行的指令。

        契约类违规是"缺少某物"，若套用"使用了被禁止的 X"的默认措辞，
        会生成"使用了被禁止的 缺少渲染契约…"这种病句 ——
        而那条文本正是模型改错的唯一线索。
        """
        report = check_html_contract("<div>静态</div>")
        feedback = report.summary()
        assert "使用了被禁止的" not in feedback, f"反馈是病句：{feedback}"
        assert "window.__seek" in feedback
        assert "window.__ready" in feedback, "应当告诉模型还需要 __ready"

    def test_cdn_feedback_is_readable(self) -> None:
        report = check_html_contract(
            "<script src='https://cdn.jsdelivr.net/d3.min.js'></script>\nwindow.__seek=(t)=>{}"
        )
        feedback = report.summary()
        assert "使用了被禁止的" not in feedback
        assert "没有网络" in feedback
        assert "SVG" in feedback or "Canvas" in feedback

    @pytest.mark.parametrize(
        "cdn",
        ["cdn.jsdelivr.net", "unpkg.com", "cdnjs.cloudflare.com", "d3js.org"],
    )
    def test_rejects_cdn_references(self, cdn: str) -> None:
        """沙盒无网络，引 CDN 会静默失败并产出空白画面。"""
        code = f'<script src="https://{cdn}/d3.min.js"></script>\nwindow.__seek=(t)=>{{}}'
        report = check_html_contract(code)
        assert not report.ok
        assert any("CDN" in v.reason for v in report.errors)

    def test_warns_on_canvas_without_context(self) -> None:
        """canvas 未取上下文是 warning（不阻断执行），但必须被观测到。"""
        report = check_html_contract('<canvas id="c"></canvas>\nwindow.__seek=(t)=>{}')
        assert report.ok
        assert any(v.severity == "warning" for v in report.violations)

    def test_rejects_empty_code(self) -> None:
        report = check_html_contract("   ")
        assert not report.ok


# ===========================================================================
# HTML 渲染器：环境不可用时的行为
# ===========================================================================


class TestBrowserProbe:
    """浏览器就绪探测必须**真的**去看二进制，而不是只 import 一下。

    回归背景：`pip install playwright` 成功、但没跑 `playwright install chromium`
    时，探测原先返回「可用」，于是编排层把 DATA/CODE 镜头派给 d3，
    渲染时才失败 —— 白烧满 attempt 才熔断。一个会说谎的就绪探测
    把「环境没准备好」伪装成「内容反复不达标」，两者该做的处置完全不同。
    """

    def setup_method(self) -> None:
        reset_browser_probe_cache()

    def teardown_method(self) -> None:
        reset_browser_probe_cache()

    @staticmethod
    def _fake_pw(executable_path: str):
        """伪造一个 playwright 上下文管理器。"""

        class _Fake:
            def __enter__(self):
                return self

            def __exit__(self, *_exc) -> bool:
                return False

        _Fake.chromium = SimpleNamespace(executable_path=executable_path)
        return lambda: _Fake()

    def test_reports_missing_browser_binary(self) -> None:
        ok, reason = _probe_browser_with(self._fake_pw("/nonexistent/chrome-for-testing"))
        assert ok is False
        assert "chromium" in reason.lower()
        # 提示里必须给出**下一步动作**，否则拿到这条信息的人仍然不知道要做什么
        assert "playwright install" in reason.lower()

    def test_reports_ready_when_binary_exists(self) -> None:
        ok, reason = _probe_browser_with(self._fake_pw(sys.executable))
        assert ok is True, reason

    def test_reports_uninitializable_playwright(self) -> None:
        def _boom():
            raise RuntimeError("driver 起不来")

        ok, reason = _probe_browser_with(_boom)
        assert ok is False
        assert "driver 起不来" in reason

    def test_result_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """缓存必须生效：健康检查按引擎各调一次，每次都起子进程太贵。

        缓存住在 `browser_ready()`（不是纯探测函数 `_probe_browser_with`），
        因此这里替换的是前者真正会调用的那一步。
        """
        pytest.importorskip("playwright.sync_api", reason="本用例要穿过 browser_ready 的真实分支")
        # 自己清掉 SCID_CHROME：它会**短路**掉探测（覆盖优先），
        # 于是这条用例在不带该变量的机器上绿、在带了的机器上红 ——
        # "只在别人机器上红"的测试比没有测试更浪费时间。本用例测的是缓存，
        # 覆盖行为由 TestBrowserOverride 单独覆盖。
        monkeypatch.delenv("SCID_CHROME", raising=False)

        calls = {"n": 0}

        def fake_probe(_factory) -> tuple[bool, str]:
            calls["n"] += 1
            return True, ""

        monkeypatch.setattr(config_module, "_probe_browser_with", fake_probe)
        reset_browser_probe_cache()

        assert browser_ready() == (True, "")
        assert browser_ready() == (True, "")
        assert calls["n"] == 1, "第二次调用应当直接吃缓存，而不是再起一次 driver"

        # 清缓存后应重新探测 —— 这是「装完浏览器不必重启」的出口
        reset_browser_probe_cache()
        browser_ready()
        assert calls["n"] == 2


class TestBrowserOverride:
    """SCID_CHROME 覆盖：策略在 `browser_ready()`，机制在 `_probe_browser_with()`。

    这条区分很重要：把覆盖塞进 `_probe_browser_with()` 会让"注入假工厂"失效
    （真实环境变量会盖掉注入的对象），于是探测机制本身没法再被单独测试 ——
    改这一处时就是被上面几条用例当场拦下来的。
    """

    def test_override_short_circuits_the_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCID_CHROME", sys.executable)
        reset_browser_probe_cache()

        def _should_not_be_called(_factory):  # noqa: ANN001, ANN202
            raise AssertionError("设置 SCID_CHROME 后不该再去问 Playwright")

        monkeypatch.setattr(config_module, "_probe_browser_with", _should_not_be_called)
        assert browser_ready() == (True, "")
        reset_browser_probe_cache()

    def test_override_pointing_nowhere_is_reported_not_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """指向不存在的路径必须**如实报不可用**，而不是无条件放行。

        "配了就放行"会让一个手滑的路径变成"引擎可用但渲染全失败"，
        那正是本项目反复强调的一类静默失败。
        """
        monkeypatch.setenv("SCID_CHROME", "/nonexistent/chrome")
        reset_browser_probe_cache()
        ok, reason = browser_ready()
        assert ok is False
        assert "SCID_CHROME" in reason
        reset_browser_probe_cache()


class TestHtmlRendererAvailability:
    def test_unavailable_environment_fails_non_retryably(self, tmp_path: Path) -> None:
        """没有 Playwright 时应该明确报"不可重试"，而不是重试到天荒地老。

        本机通常没装 Playwright，因此这条用例在开发机上就是真实路径；
        装了的话下面的断言会自动跳过（available() 会返回 True）。
        """
        renderer = HtmlRenderer(make_settings(tmp_path), "d3")
        ok, reason = renderer.available()
        if ok:
            pytest.skip("本机已安装 Playwright，跳过不可用路径")

        assert "playwright" in reason.lower() or "ffmpeg" in reason.lower()
        with pytest.raises(RendererError) as exc_info:
            renderer.render(make_request(tmp_path, code="window.__seek=(t)=>{}"), SandboxRunner())
        assert exc_info.value.retryable is False


# ===========================================================================
# 内部工具
# ===========================================================================


class TestColorHelpers:
    def test_hex_to_ffmpeg(self) -> None:
        from scidirector_ai.renderer import _hex_to_ffmpeg

        assert _hex_to_ffmpeg("#4F8CFF") == "0x4F8CFF"
        assert _hex_to_ffmpeg("4f8cff") == "0x4F8CFF"
        assert _hex_to_ffmpeg("#ABC") == "0xAABBCC"

    def test_invalid_color_falls_back_instead_of_failing(self) -> None:
        """非法颜色回退到默认色，而不是让整个镜头渲染失败。

        风格约束来自外部输入，一个拼错的颜色不该毁掉一次渲染。
        """
        from scidirector_ai.renderer import _hex_to_ffmpeg, _shift

        assert _hex_to_ffmpeg("not-a-color") == "0x4F8CFF"
        assert _hex_to_ffmpeg("") == "0x4F8CFF"
        assert _shift("bogus") == "#FF6B6B"

    def test_shift_moves_towards_warm(self) -> None:
        from scidirector_ai.renderer import _shift

        shifted = _shift("#4F8CFF")
        assert shifted.startswith("#") and len(shifted) == 7
        # 红色分量应当变大（向暖色偏移）。
        assert int(shifted[1:3], 16) > 0x4F


class TestHtmlWrapping:
    def test_full_document_is_passed_through(self, tmp_path: Path) -> None:
        from scidirector_ai.renderer import _wrap_html

        doc = "<!doctype html><html><body>hi</body></html>"
        assert _wrap_html(make_request(tmp_path, code=doc)) == doc

    def test_fragment_is_wrapped_with_theme(self, tmp_path: Path) -> None:
        from scidirector_ai.renderer import _wrap_html

        wrapped = _wrap_html(
            make_request(
                tmp_path, code="<script>window.__seek=(t)=>{}</script>",
                background_color="#123456",
            )
        )
        assert wrapped.startswith("<!doctype html>")
        assert "#123456" in wrapped, "背景色未注入外壳"
        assert "window.__seek" in wrapped
        assert 'id="stage"' in wrapped
