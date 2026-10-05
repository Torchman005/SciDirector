"""Avoid spending render and vision calls on revisions with no visible change."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scidirector_ai.agents.coder import CodeArtifact, CodeGenerationResult
from scidirector_ai.agents.critic import CritiqueOutcome
from scidirector_ai.graph import nodes as nodes_module
from scidirector_ai.graph.nodes import (
    HINT_HUMAN, HINT_RENDER, HINT_RETRY, PipelineNodes, _collect_feedback,
    _same_unresolved_feedback,
)
from scidirector_ai.graph.state import initial_state
from scidirector_ai.llm import LLMError
from scidirector_ai.media import FrameSample
from scidirector_ai.schemas import (
    CriticFeedback, FeedbackSource, RenderArtifact, SceneTag, ShotSpec, StyleGuide,
)


def _state() -> dict[str, Any]:
    state = initial_state(
        job_id="job-r", raw_script="script", style_guide=StyleGuide(),
        target_duration_sec=5, max_attempts_per_shot=3, locale="zh-CN",
    )
    shot = ShotSpec(shot_id="job-r-s000", index=0, narration="narration",
                    visual_brief="figure", tag=SceneTag.MATH, duration_sec=5)
    state.update(shots=[shot], attempts={shot.shot_id: 1}, current_code="old code",
                 feedback={shot.shot_id: CriticFeedback(
                     passed=False, score=0.6, suggestions=["make figure bigger"]
                 )})
    return state


def _deps(coder: Any = None, renderer: Any = None, critic: Any = None) -> Any:
    settings = SimpleNamespace(
        sandbox_work_dir="unused", render_width=320, render_height=240,
        render_fps=10, critic_frame_samples=3, critic_frame_width=320,
    )
    return SimpleNamespace(
        coder=coder, critic=critic, renderer=lambda engine: renderer,
        settings=settings, runner=object(),
        llm=SimpleNamespace(usage=SimpleNamespace(total_tokens=0)),
    )


def test_identical_source_skips_render() -> None:
    class Coder:
        def generate(self, **kwargs: Any) -> CodeGenerationResult:
            return CodeGenerationResult(
                artifact=CodeArtifact(code="old code", language="python"),
                policy_ok=True, policy_summary="",
            )

    update = PipelineNodes(_deps(coder=Coder())).code(_state())
    assert update["route_hint"] == HINT_RETRY
    assert update["attempts"]["job-r-s000"] == 2
    assert "跳过无效渲染与审查" in update["events"][0]["message"]
    assert "新源码与上一轮" in update["render_error"]
    state = _state()
    state.update(update)
    assert "【无效重做反馈】" in _collect_feedback(state, "job-r-s000")


def test_identical_source_reaches_attempt_limit_without_render() -> None:
    class Coder:
        def generate(self, **kwargs: Any) -> CodeGenerationResult:
            return CodeGenerationResult(
                artifact=CodeArtifact(code="old code", language="python"),
                policy_ok=True, policy_summary="",
            )

    nodes = PipelineNodes(_deps(coder=Coder()))
    state = _state()
    for expected_attempt in (2, 3):
        update = nodes.code(state)
        assert update["attempts"]["job-r-s000"] == expected_attempt
        state.update(update)
        decision = nodes.revise(state)
        assert decision["route_hint"] == (HINT_RETRY if expected_attempt == 2 else HINT_HUMAN)


def test_same_source_can_retry_transient_render_failure() -> None:
    class Coder:
        def generate(self, **kwargs: Any) -> CodeGenerationResult:
            return CodeGenerationResult(
                artifact=CodeArtifact(code="old code", language="python"),
                policy_ok=True, policy_summary="",
            )

    state = _state()
    state["render_error"] = "ffmpeg timeout"
    assert PipelineNodes(_deps(coder=Coder())).code(state)["route_hint"] == HINT_RENDER


def test_coder_failure_consumes_attempt() -> None:
    class Coder:
        def generate(self, **kwargs: Any) -> None:
            raise LLMError("provider unavailable")

    update = PipelineNodes(_deps(coder=Coder())).code(_state())
    assert update["route_hint"] == HINT_RETRY
    assert update["attempts"]["job-r-s000"] == 2


def test_identical_review_frames_skip_vision(monkeypatch: pytest.MonkeyPatch) -> None:
    class Renderer:
        def render(self, request: Any, runner: Any) -> Any:
            return SimpleNamespace(video_path="new.mp4", duration_sec=5)

    monkeypatch.setattr(nodes_module, "extract_frames_with_times", lambda *args, **kwargs: [
        FrameSample(path="first.jpg", ts=0.1), FrameSample(path="last.jpg", ts=4.9),
    ])
    monkeypatch.setattr(nodes_module, "_frame_signatures", lambda paths: ["a", "b"])
    state = _state()
    state["attempts"]["job-r-s000"] = 2
    state["frame_signatures"] = {"job-r-s000": ["a", "b"]}
    update = PipelineNodes(_deps(renderer=Renderer())).render(state)
    assert update["route_hint"] == HINT_RETRY
    assert "跳过重复视觉审查" in update["events"][0]["message"]
    assert "可见效果" in update["render_error"]


def test_vision_outage_does_not_trigger_frame_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    class Renderer:
        def render(self, request: Any, runner: Any) -> Any:
            return SimpleNamespace(video_path="new.mp4", duration_sec=5,
                                   width=320, height=240, fps=10, engine="manim",
                                   render_cost_sec=0.1)

    monkeypatch.setattr(nodes_module, "extract_frames_with_times", lambda *args, **kwargs: [
        FrameSample(path="first.jpg", ts=0.1), FrameSample(path="last.jpg", ts=4.9),
    ])
    monkeypatch.setattr(nodes_module, "_frame_signatures", lambda paths: ["a", "b"])
    monkeypatch.setattr(nodes_module, "analyze_motion", lambda *args, **kwargs: nodes_module.MotionReport(
        ratios=[], static_spans=[], min_change_ratio=0.005,
    ))
    state = _state()
    state["attempts"]["job-r-s000"] = 2
    state["frame_signatures"] = {"job-r-s000": ["a", "b"]}
    state["feedback"]["job-r-s000"].source = FeedbackSource.SYSTEM
    nodes = PipelineNodes(_deps(renderer=Renderer()))
    monkeypatch.setattr(nodes, "_synthesize_narration", lambda *args: ("", ""))
    update = nodes.render(state)
    assert update["route_hint"] != HINT_RETRY


def test_frame_signature_uses_contents_not_reused_names(monkeypatch: pytest.MonkeyPatch) -> None:
    data = {"frame.jpg": b"first render"}
    monkeypatch.setattr(Path, "read_bytes", lambda path: data[path.name])
    first = nodes_module._frame_signatures(["frame.jpg"])
    data["frame.jpg"] = b"second render"
    assert nodes_module._frame_signatures(["frame.jpg"]) != first


def test_repeated_feedback_prompts_structural_revision() -> None:
    old = CriticFeedback(passed=False, score=0.65, issues=["Text too small"],
                         suggestions=["Make text larger"])
    repeat = CriticFeedback(passed=False, score=0.66, issues=[" text  too small "],
                            suggestions=["Make text larger"])
    improved = CriticFeedback(passed=False, score=0.72, issues=["Text too small"],
                              suggestions=["Make text larger"])
    assert _same_unresolved_feedback(old, repeat)
    paraphrase = CriticFeedback(passed=False, score=0.66,
                                issues=["Text is too small"],
                                suggestions=["Make text larger"])
    assert _same_unresolved_feedback(old, paraphrase)
    assert not _same_unresolved_feedback(old, improved)
    assert not _same_unresolved_feedback(old, CriticFeedback(
        passed=False, score=0.66, issues=["Overlapping labels"],
        suggestions=["Separate labels"],
    ))
    state = _state()
    state["revision_stagnation"] = {"job-r-s000": "必须重构相关画面元素"}
    assert "必须重构相关画面元素" in _collect_feedback(state, "job-r-s000")
    assert "必须重构相关画面元素" not in _collect_feedback(state, "another-shot")


def test_critique_records_stagnation_for_next_coder_call() -> None:
    class Critic:
        def review(self, **kwargs: Any) -> CritiqueOutcome:
            return CritiqueOutcome(feedback=CriticFeedback(
                passed=False, score=0.61, issues=["figure too small"],
                suggestions=["enlarge the figure"],
            ))

    state = _state()
    state["feedback"]["job-r-s000"] = CriticFeedback(
        passed=False, score=0.60, issues=["figure too small"],
        suggestions=["enlarge the figure"],
    )
    state["artifacts"] = {"job-r-s000": RenderArtifact(shot_id="job-r-s000")}
    update = PipelineNodes(_deps(critic=Critic())).critique(state)
    assert "必须重构相关画面元素" in update["revision_stagnation"]["job-r-s000"]
    assert "连续两轮问题相同" in update["events"][0]["message"]
