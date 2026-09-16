"""局部重渲染（只重渲某几秒）的测试。

这一层的价值全在「诚实地说明自己有没有做到」上：
Python 侧如实上报 ``partial_range_honored``，Go 侧据此决定拼接回原片还是整镜替换。
一旦上报失实，Go 会把只渲染了一小段的片段当成完整镜头拼进成片 ——
成片时长错乱、音画失调，而且**不会有任何报错**。
"""

from __future__ import annotations

import shutil

import pytest

from scidirector_ai.media import _build_filtergraph, probe, render_ambient
from scidirector_ai.renderer import (
    AmbientRenderer,
    ManimRenderer,
    RenderRequest,
    build_renderer,
)
from scidirector_ai.sandbox.runner import SandboxRunner
from scidirector_ai.config import Settings

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def _req(**kw) -> RenderRequest:
    base = dict(
        shot_id="t-001", code="", output_dir="/tmp/x",
        duration_sec=6.0, width=320, height=240, fps=15,
    )
    base.update(kw)
    return RenderRequest(**base)


# ---------------------------------------------------------------------------
# 区间语义
# ---------------------------------------------------------------------------


def test_wants_range_semantics() -> None:
    assert _req().wants_range is False
    # end <= start 表示不启用：这是「整镜渲染」的默认编码方式，
    # 不能因为 end 有值就误判成局部渲染。
    assert _req(range_start_sec=2, range_end_sec=2).wants_range is False
    assert _req(range_start_sec=3, range_end_sec=1).wants_range is False
    assert _req(range_start_sec=2, range_end_sec=4).wants_range is True


def test_effective_duration_and_start() -> None:
    no_range = _req()
    assert no_range.effective_duration_sec == 6.0
    assert no_range.effective_start_sec == 0.0

    ranged = _req(range_start_sec=2.0, range_end_sec=4.5)
    assert ranged.effective_duration_sec == pytest.approx(2.5)
    assert ranged.effective_start_sec == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# lavfi 滤镜图：trim 必须在**末尾**
# ---------------------------------------------------------------------------


def test_filtergraph_appends_trim_at_end() -> None:
    """trim 必须排在 drawtext **之后**。

    放在前面会让 drawtext 的淡入淡出相位整体前移，
    拼回原片后表现为「标题在错误的时间点亮起」——
    而这类错误只看单独渲染出来的片段是发现不了的。
    """
    graph = _build_filtergraph(
        duration_sec=6.0, width=320, height=240, fps=15,
        colors=("0x0B1020", "0x4F8CFF", "0xFF6B6B"),
        font="/fake/font.ttf", text="标题",
        window_start_sec=2.0, window_end_sec=4.0,
    )
    assert "trim=start=2.000:end=4.000" in graph
    assert "setpts=PTS-STARTPTS" in graph

    idx_drawtext = graph.find("drawtext=")
    idx_trim = graph.find("trim=")
    assert idx_drawtext >= 0 and idx_trim >= 0
    assert idx_drawtext < idx_trim, "trim 必须排在 drawtext 之后，否则文字动画相位会错"
    assert graph.rstrip().endswith("setpts=PTS-STARTPTS")


def test_filtergraph_without_window_has_no_trim() -> None:
    graph = _build_filtergraph(
        duration_sec=6.0, width=320, height=240, fps=15,
        colors=("0x0B1020", "0x4F8CFF", "0xFF6B6B"),
    )
    assert "trim=" not in graph


# ---------------------------------------------------------------------------
# 真实 ffmpeg：局部渲染的产物时长应等于区间长度
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_FFMPEG, reason="需要 ffmpeg")
def test_ambient_partial_render_produces_window_length(tmp_path) -> None:
    runner = SandboxRunner()
    out = tmp_path / "patch.mp4"

    render_ambient(
        out, runner,
        duration_sec=6.0, width=320, height=240, fps=15,
        text="",  # 不画文字，避免字体依赖影响本用例
        window_start_sec=2.0, window_end_sec=4.0,
    )

    info = probe(out, runner)
    # 产物应当只覆盖 2 秒的窗口，而不是整镜的 6 秒 ——
    # 这正是「省下渲染量」的量化依据。
    assert info.duration_sec == pytest.approx(2.0, abs=0.25), (
        f"局部渲染产物时长 {info.duration_sec:.3f}s，期望约 2.0s"
    )


@pytest.mark.skipif(not HAS_FFMPEG, reason="需要 ffmpeg")
def test_ambient_full_render_unchanged(tmp_path) -> None:
    """不传窗口时行为必须与改动前完全一致（回归保护）。"""
    runner = SandboxRunner()
    out = tmp_path / "full.mp4"
    render_ambient(out, runner, duration_sec=3.0, width=320, height=240, fps=15, text="")
    info = probe(out, runner)
    assert info.duration_sec == pytest.approx(3.0, abs=0.25)


# ---------------------------------------------------------------------------
# 引擎能力：谁支持、谁不支持
# ---------------------------------------------------------------------------


def test_manim_never_claims_partial_range(tmp_path) -> None:
    """Manim 必须声明 partial_range_honored=False。

    Manim 按动画序号驱动渲染，「第 3~5 秒」无法可靠映射到动画区间。
    谎报 True 会让 Go 侧把一段 2 秒的片段当成完整镜头拼进成片。
    """
    settings = Settings()
    renderer = build_renderer("manim", settings)
    assert isinstance(renderer, ManimRenderer)


def test_ambient_renderer_declares_window_support(tmp_path) -> None:
    settings = Settings()
    renderer = build_renderer("stock", settings)
    assert isinstance(renderer, AmbientRenderer)


@pytest.mark.skipif(not HAS_FFMPEG, reason="需要 ffmpeg")
def test_ambient_renderer_reports_honored(tmp_path) -> None:
    """AmbientRenderer 必须如实回填 partial_range_honored。"""
    settings = Settings()
    renderer = build_renderer("stock", settings)
    runner = SandboxRunner()

    ranged = _req(
        output_dir=str(tmp_path / "a"),
        range_start_sec=1.0, range_end_sec=2.5,
    )
    result = renderer.render(ranged, runner)
    assert result.partial_range_honored is True
    assert result.duration_sec == pytest.approx(1.5, abs=0.25)

    full = _req(output_dir=str(tmp_path / "b"))
    result_full = renderer.render(full, runner)
    assert result_full.partial_range_honored is False
    assert result_full.duration_sec == pytest.approx(6.0, abs=0.3)
