"""A stable repair ledger must close only with task-specific current evidence."""
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scidirector_ai.agents.critic import CriticAgent, _RawCritique, _RepairResult
from scidirector_ai.config import Settings
from scidirector_ai.review_tasks import merge_repairs
from scidirector_ai.schemas import CriticFeedback, RenderArtifact, SceneTag, ShotSpec, StyleGuide
from test_repair_tasks import task


def previous() -> CriticFeedback:
    return CriticFeedback(passed=False, suggestions=["repair"], repair_tasks=[
        task(task_id=f"r1-{i}", target=f"labels {i}") for i in range(4)])


def test_missing_results_and_unselected_tasks_cannot_disappear() -> None:
    old = previous()
    result = merge_repairs(old, CriticFeedback(passed=True, score=.95), [], set())
    assert not result.passed and len(result.repair_tasks) == 4
    assert result.repair_tasks[0].status == "unverified"
    assert result.repair_tasks[3] == old.repair_tasks[3]


def test_verified_result_keeps_original_acceptance_and_id() -> None:
    old = previous()
    result = merge_repairs(old, CriticFeedback(passed=True, score=.95),
                           [{"task_id": "r1-0", "status": "resolved", "evidence": "labels separated"}], {"r1-0"})
    assert result.repair_tasks[0].status == "resolved"
    assert result.repair_tasks[0].acceptance == old.repair_tasks[0].acceptance
    assert result.repair_tasks[0].task_id == "r1-0" and not result.passed
    repeated = task(task_id="r2-1", target="labels 0")
    again = merge_repairs(old, CriticFeedback(passed=False, suggestions=["repair"], repair_tasks=[repeated]),
                          [{"task_id": "r1-0", "status": "resolved", "evidence": "done"}], {"r1-0"})
    assert len(again.repair_tasks) == 4 and again.repair_tasks[0].status == "open"


@pytest.mark.parametrize("evidence,indices,expected", [
    ("labels separately readable in after image", [4], True),
    ("", [4], False), ("done", [3], False), ("done", [999], False),
    ("done", [1], False),
])
def test_review_uses_one_call_and_requires_current_pair(
    tmp_path: Path, evidence: str, indices: list[int], expected: bool,
) -> None:
    before, after = tmp_path / "before.png", tmp_path / "after.png"
    before.write_bytes(b"before")
    after.write_bytes(b"after")
    calls: list[dict[str, Any]] = []
    def vision(system: str, user: str, schema: Any, **kwargs: Any) -> _RawCritique:
        calls.append(kwargs)
        assert "本轮逐项复审" in user and "labels independently readable" in user
        return _RawCritique(passed=True, logic_score=.95, readability_score=.95,
                            pacing_score=.95, aesthetics_score=.95, repair_results=[
                                _RepairResult(task_id="original", status="resolved", evidence=evidence,
                                              image_indices=indices)])
    old = CriticFeedback(passed=False, suggestions=["repair"], repair_tasks=[task(task_id="original")])
    outcome = CriticAgent(SimpleNamespace(vision_json=vision), Settings(env="test", llm_provider="mock")).review(
        shot=ShotSpec(shot_id="s", tag=SceneTag.MATH, duration_sec=5),
        artifact=RenderArtifact(duration_sec=5, frame_samples=["a", "b"]),
        style_guide=StyleGuide(), attempt=2, previous_review=old,
        repair_prechecks=[{"task_id": "original", "status": "changed", "pairs": [
            {"ts": 1, "before": str(before), "after": str(after)}]}])
    assert outcome.passed == expected and len(calls) == 1
    assert calls[0]["images"] == ["a", "b", str(before), str(after)]
    assert "历史版本 BEFORE" in calls[0]["image_labels"][2]
    assert "当前版本 AFTER" in calls[0]["image_labels"][3]
    assert outcome.feedback.repair_tasks[0].task_id == "original"


def test_omitted_repair_results_get_one_clarification_without_rerender(tmp_path: Path):
    before, after = tmp_path/"before.png",tmp_path/"after.png"
    before.write_bytes(b"before"); after.write_bytes(b"after")
    calls=[]
    def vision(system,user,schema,**kwargs):
        calls.append(user)
        assert "不得省略 repair_results" in system
        results=[] if len(calls)==1 else [_RepairResult(task_id="original",status="resolved",
            evidence="当前 AFTER 图4中标签清晰分开",image_indices=[4])]
        return _RawCritique(passed=True,logic_score=.95,readability_score=.95,pacing_score=.9,
                            aesthetics_score=.9,repair_results=results)
    old=CriticFeedback(passed=False,suggestions=["repair"],repair_tasks=[task(task_id="original")])
    outcome=CriticAgent(SimpleNamespace(vision_json=vision),Settings()).review(
        shot=ShotSpec(shot_id="s",tag=SceneTag.MATH,duration_sec=5),
        artifact=RenderArtifact(duration_sec=5,frame_samples=["a","b"]),style_guide=StyleGuide(),
        attempt=2,previous_review=old,repair_prechecks=[{"task_id":"original","status":"changed",
          "pairs":[{"ts":1,"before":str(before),"after":str(after)}]}])
    assert outcome.passed and len(calls)==2
    assert outcome.feedback.repair_tasks[0].status=="resolved"
