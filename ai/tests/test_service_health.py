"""健康与能力探测的单元测试。

这一组测试守护的是**运维语义**：`/readyz` 返回什么，直接决定编排系统
要不要把流量摘走。判错方向的后果是「一个什么镜头都产不出来的实例仍在接流量」，
或者反过来「一个其实能干活（只有 ffmpeg）的实例被反复重启」。

测试用 monkeypatch 固定工具链探测结果，**不依赖测试机实际装了什么** ——
否则同一份代码在开发机与 CI 上会给出不同结论，那是最难排查的一类失败。
"""

from __future__ import annotations

import pytest

from scidirector_ai.config import ENGINE_REQUIREMENTS, Settings, engine_availability
from scidirector_ai.service import PipelineService


# ---------------------------------------------------------------------------
# 纯函数：工具链 -> 引擎可用性
# ---------------------------------------------------------------------------


class TestEngineAvailability:
    """``engine_availability`` 是纯函数，因此可以穷举验证。"""

    def test_all_tools_present(self) -> None:
        toolchain = {"python": True, "ffmpeg": True, "ffprobe": True,
                     "latex": True, "manim": True, "playwright": True}
        engines = engine_availability(toolchain)
        assert set(engines) == set(ENGINE_REQUIREMENTS)
        assert all(engines.values()), engines

    def test_only_ffmpeg_means_only_stock(self) -> None:
        """只有 ffmpeg 时，氛围镜头仍可渲染 —— 这正是"至少一个引擎"判定的由来。"""
        engines = engine_availability({"ffmpeg": True})
        assert engines["stock"] is True
        assert engines["manim"] is False
        assert engines["d3"] is False
        assert engines["echarts"] is False
        assert engines["code_anim"] is False

    def test_nothing_available(self) -> None:
        engines = engine_availability({})
        assert not any(engines.values())

    def test_manim_needs_latex_and_ffmpeg(self) -> None:
        """Manim 缺 LaTeX 时无法渲染公式 —— 这是它最常见的"看起来装了却用不了"。"""
        engines = engine_availability({"manim": True, "ffmpeg": True, "latex": False})
        assert engines["manim"] is False

        engines = engine_availability({"manim": True, "ffmpeg": True, "latex": True})
        assert engines["manim"] is True

    def test_browser_engines_need_playwright(self) -> None:
        engines = engine_availability({"ffmpeg": True, "playwright": False})
        assert engines["d3"] is False
        assert engines["code_anim"] is False

        engines = engine_availability({"ffmpeg": True, "playwright": True})
        assert engines["d3"] is True
        assert engines["echarts"] is True
        assert engines["code_anim"] is True

    def test_missing_keys_are_treated_as_unavailable(self) -> None:
        """探测结果缺键时按"不可用"处理，绝不按"可用"处理。

        方向很重要：把缺项当成可用，会让系统在真正缺依赖时仍然宣称自己就绪。
        """
        engines = engine_availability({"ffmpeg": True, "manim": True})  # 没给 latex
        assert engines["manim"] is False


# ---------------------------------------------------------------------------
# 健康报告
# ---------------------------------------------------------------------------


@pytest.fixture()
def service(monkeypatch: pytest.MonkeyPatch) -> PipelineService:
    """构造一个工具链确定可控的 PipelineService。"""
    settings = Settings(env="test", llm_provider="mock")
    svc = PipelineService(settings)
    return svc


def _patch_toolchain(monkeypatch: pytest.MonkeyPatch, toolchain: dict[str, bool]) -> None:
    monkeypatch.setattr(Settings, "toolchain_report", lambda self: dict(toolchain), raising=True)


class TestHealthReport:
    def test_sandbox_ready_is_false_when_no_engine_available(
        self, service: PipelineService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """所有引擎都不可用 -> 不该声称就绪（这正是修复前的缺陷所在）。"""
        _patch_toolchain(monkeypatch, {"python": True, "ffmpeg": False, "ffprobe": False,
                                       "latex": False, "manim": False, "playwright": False})
        status = service.health()
        assert status.sandbox_ready is False
        assert not any(status.engines.values())
        assert "render" not in status.capabilities

    def test_sandbox_ready_is_true_with_ffmpeg_only(
        self, service: PipelineService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """只有 ffmpeg 时仍是"可干活"的（氛围镜头），不该被判成 not-ready。"""
        _patch_toolchain(monkeypatch, {"python": True, "ffmpeg": True, "ffprobe": True,
                                       "latex": False, "manim": False, "playwright": False})
        status = service.health()
        assert status.sandbox_ready is True
        assert status.engines["stock"] is True
        assert status.engines["manim"] is False

    def test_capabilities_expose_each_engine(self, service: PipelineService,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
        """逐引擎的可用性必须能从 capabilities 里读出来。

        编排层据此提前知道"哪些标签当前渲染不了"，而不是等任务跑到那一步才失败。
        """
        _patch_toolchain(monkeypatch, {"python": True, "ffmpeg": True, "ffprobe": True,
                                       "latex": True, "manim": True, "playwright": False})
        status = service.health()
        caps = set(status.capabilities)
        assert "engine:manim=ok" in caps
        assert "engine:stock=ok" in caps
        assert "engine:d3=missing" in caps
        assert "engine:code_anim=missing" in caps

    def test_sandbox_ready_matches_any_engine(
        self, service: PipelineService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """不变式：sandbox_ready 必须恒等于 any(engines.values())。

        把这条写成断言而不是写死期望值，是为了让"引擎集合变化"时
        这条语义约束依然被守住。
        """
        for toolchain in (
            {},
            {"ffmpeg": True},
            {"ffmpeg": True, "playwright": True},
            {"ffmpeg": True, "manim": True, "latex": True},
            {"python": True, "ffmpeg": True, "ffprobe": True,
             "latex": True, "manim": True, "playwright": True},
        ):
            _patch_toolchain(monkeypatch, toolchain)
            status = service.health()
            assert status.sandbox_ready == any(status.engines.values()), toolchain

    def test_health_reports_mock_provider(self, service: PipelineService,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_toolchain(monkeypatch, {"ffmpeg": True})
        status = service.health()
        assert status.llm_provider == "mock"
        assert "mock-llm" in status.capabilities

    def test_toolchain_is_passed_through(self, service: PipelineService,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
        raw = {"python": True, "ffmpeg": True, "ffprobe": True,
               "latex": False, "manim": False, "playwright": False}
        _patch_toolchain(monkeypatch, raw)
        status = service.health()
        assert status.toolchain == raw
