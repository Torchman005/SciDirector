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
    # 仍缺定位时不再转人工：判负理由转成整段任务继续自动修复。
    outcome, calls = review([raw(repair_tasks=[])])
    assert not outcome.degraded and len(calls) == 2
    fallback = outcome.feedback.repair_tasks
    assert fallback and fallback[0].target == "整段画面" and fallback[0].start_sec == 0
    assert "labels overlap" in fallback[0].evidence


def test_invalid_frame_is_clamped_to_whole_shot() -> None:
    outcome, calls = review([raw(repair_tasks=[task(frame_indices=[9])])])
    assert not outcome.degraded
    assert all(i <= 2 for t in outcome.feedback.repair_tasks for i in t.frame_indices)


def test_out_of_range_region_does_not_fail_parse() -> None:
    from scidirector_ai.schemas import RepairTask
    fixed = RepairTask(target="#a", evidence="e", instruction="i", acceptance="a",
                       start_sec=3, end_sec=1, region=[0.9, 0.5, 0.3, 0.2])
    assert (fixed.start_sec, fixed.end_sec) == (1, 3)
    assert abs(fixed.region[0] + fixed.region[2] - 1) < 1e-9


def test_blocking_error_vetoes_high_score() -> None:
    outcome, _ = review([raw(passed=True, logic_score=0.95, readability_score=0.95,
                             repair_tasks=[task(category="logic", severity="blocking")])])
    assert not outcome.passed and outcome.feedback.repair_tasks


@pytest.mark.parametrize("changes,check", [
    (dict(start_sec=2, end_sec=1), lambda t: (t.start_sec, t.end_sec) == (1, 2)),
    (dict(frame_indices=[0, 2]), lambda t: t.frame_indices == [2]),
    (dict(region=[0.9, 0.1, 0.2, 0.2]), lambda t: abs(t.region[2] - 0.1) < 1e-9),
])
def test_invalid_coordinates_are_repaired_not_rejected(changes: dict[str, Any], check) -> None:
    # 单项坐标越界不能让整份审核解析失败、白白烧掉一次重试。
    assert check(task(**changes))


def test_reworded_issue_matches_existing_task_by_element_id() -> None:
    from scidirector_ai.review_tasks import merge_repairs
    old = CriticFeedback(passed=False, suggestions=["x"], repair_tasks=[
        task(task_id="r1-01", target="#label_a 与 #curve 重叠", acceptance="标签与曲线分开")])
    reworded = task(target="#label_a 压在曲线上", acceptance="标签完整可读且不压线")
    merged = merge_repairs(old, CriticFeedback(passed=False, score=.5, suggestions=["x"],
                                               repair_tasks=[reworded]), [], set())
    assert [t.task_id for t in merged.repair_tasks] == ["r1-01"]
    assert merged.repair_tasks[0].status == "open"


def test_unverified_result_does_not_count_as_failure() -> None:
    from scidirector_ai.review_tasks import update_repair_progress
    feedback = CriticFeedback(passed=False, suggestions=["x"], repair_tasks=[task(task_id="r1-01")])
    progress, _, exhausted = update_repair_progress(feedback, {}, 1, 3)
    for attempt in (2, 3):
        feedback.repair_tasks[0].status = "unverified"
        progress, _, exhausted = update_repair_progress(feedback, progress, attempt, 5)
    assert not exhausted and progress["r1-01"]["failures"] == 0


def test_human_comment_becomes_blocking_task_until_explicitly_resolved() -> None:
    from scidirector_ai.review_tasks import ensure_human_task, format_repairs, merge_repairs
    previous = ensure_human_task(None, "公式出现得太早，请先讲图再出公式", attempt=3, duration=6)
    assert previous.repair_tasks[0].category == "human" and previous.repair_tasks[0].end_sec == 6
    assert format_repairs(previous).startswith("【审核员意见（优先级最高")
    # 机器复审高分通过但没有逐项确认：审核员意见不能被静默吞掉。
    merged = merge_repairs(previous, CriticFeedback(passed=True, score=.95), [], set())
    assert not merged.passed and merged.repair_tasks[0].status != "resolved"
    done = merge_repairs(previous, CriticFeedback(passed=True, score=.95),
                         [{"task_id": previous.repair_tasks[0].task_id, "status": "resolved",
                           "evidence": "公式在图示之后出现"}], {previous.repair_tasks[0].task_id})
    assert done.passed
    # 同一意见重复提交不重复登记；自动修复请求不是审核员意见。
    assert len(ensure_human_task(previous, "公式出现得太早，请先讲图再出公式", attempt=4).repair_tasks) == 1
    assert ensure_human_task(None, "自动诊断并修复：根据系统错误修复", attempt=1) is None


def test_major_layout_task_is_advice_when_score_passes() -> None:
    from scidirector_ai.review_tasks import merge_repairs
    fresh = task(severity="major", category="layout")
    merged = merge_repairs(None, CriticFeedback(passed=True, score=.9, repair_tasks=[fresh]), [], set())
    assert merged.passed
    logic = task(severity="major", category="logic")
    assert not merge_repairs(None, CriticFeedback(passed=True, score=.9, repair_tasks=[logic]), [], set()).passed
