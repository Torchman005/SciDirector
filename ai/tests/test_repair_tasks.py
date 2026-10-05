"""Repairs must carry evidence across processes and focus each retry."""
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from scidirector_ai import pbconv
from scidirector_ai.agents.critic import CriticAgent, CritiqueOutcome, _RawCritique
from scidirector_ai.config import Settings
from scidirector_ai.review_tasks import active_repairs, format_repairs
from scidirector_ai.schemas import CriticFeedback, RepairTask, RenderArtifact, SceneTag, ShotSpec, StyleGuide


def task(**changes: Any) -> RepairTask:
    values = dict(task_id="r1-01", category="readability", severity="major", frame_indices=[2],
                  target="labels", evidence="labels overlap in frame 2", instruction="separate labels",
                  acceptance="labels independently readable")
    return RepairTask(**{**values, **changes})


def raw(**changes: Any) -> _RawCritique:
    values = dict(passed=False, score=0.5, logic_score=0.8, readability_score=0.3,
                  pacing_score=0.8, aesthetics_score=0.8, issues=["labels overlap"],
                  suggestions=["separate labels"], repair_tasks=[task()])
    return _RawCritique(**{**values, **changes})


def review(responses: list[_RawCritique]) -> tuple[CritiqueOutcome, list[str]]:
    calls: list[str] = []
    def vision(system: str, user: str, *args: Any, **kwargs: Any) -> _RawCritique:
        calls.append(user)
        return responses[min(len(calls) - 1, len(responses) - 1)]
    agent = CriticAgent(SimpleNamespace(vision_json=vision), Settings(env="test", llm_provider="mock"))
    outcome = agent.review(shot=ShotSpec(shot_id="s0", tag=SceneTag.MATH, duration_sec=5),
                           artifact=RenderArtifact(duration_sec=5, width=320, frame_samples=["a", "b"]),
                           style_guide=StyleGuide(), attempt=1)
    return outcome, calls


def test_proto_roundtrip() -> None:
    original = CriticFeedback(passed=False, score=0.6, suggestions=["separate labels"],
                              repair_tasks=[task(region=[0.2, 0.2, 0.3, 0.3])], fatal_issues=["formula error"])
    back = pbconv.feedback_from_pb(pbconv.feedback_to_pb(original))
    assert back.repair_tasks == original.repair_tasks
    assert back.fatal_issues == original.fatal_issues


def test_batch_prioritizes_three() -> None:
    feedback = CriticFeedback(passed=False, suggestions=["repair"], repair_tasks=[
        task(task_id="advice", severity="advisory"), task(task_id="major1"),
        task(task_id="block", severity="blocking"), task(task_id="major2"), task(task_id="major3")])
    assert [t.task_id for t in active_repairs(feedback)] == ["block", "major1", "major2"]
    assert "independently readable" in format_repairs(feedback)
    assert "major3" not in format_repairs(feedback)
    assert len(feedback.repair_tasks) == 5


def test_clarification_is_bounded() -> None:
    outcome, calls = review([raw(repair_tasks=[]), raw()])
    assert len(calls) == 2 and "补充真实帧号" in calls[-1]
    assert not outcome.degraded and not outcome.passed
    assert outcome.feedback.repair_tasks[0].task_id == "r1-01"
    outcome, calls = review([raw(repair_tasks=[])])
    assert outcome.degraded and len(calls) == 2
    assert "定位" in outcome.degradation_reason


def test_invalid_frame_goes_to_human() -> None:
    outcome, calls = review([raw(repair_tasks=[task(frame_indices=[9])])])
    assert outcome.degraded and len(calls) == 2


def test_blocking_error_vetoes_high_score() -> None:
    outcome, _ = review([raw(passed=True, logic_score=0.95, readability_score=0.95,
                             repair_tasks=[task(category="logic", severity="blocking")])])
    assert not outcome.passed and outcome.feedback.repair_tasks


@pytest.mark.parametrize("changes", [dict(start_sec=2, end_sec=1), dict(frame_indices=[0]),
                                     dict(region=[0.9, 0.1, 0.2, 0.2])])
def test_invalid_coordinates(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        task(**changes)
