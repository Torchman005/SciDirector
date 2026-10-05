"""Repeated task failures get one different strategy within the original budget."""
import json
from typing import Any

import pytest

from scidirector_ai.graph.nodes import HINT_HUMAN, HINT_RETRY, PipelineNodes, _collect_feedback
from scidirector_ai.review_tasks import REPAIR_STRATEGIES, update_repair_progress
from scidirector_ai.schemas import CriticFeedback
from test_repair_tasks import task
from test_retry_efficiency import _deps, _state


@pytest.mark.parametrize("category", list(REPAIR_STRATEGIES))
def test_escalation_gets_one_attempt_then_handoff(category: str) -> None:
    state = _state()
    sid = state["shots"][0].shot_id
    state["max_attempts_per_shot"] = 5
    state["feedback"][sid] = CriticFeedback(passed=False, score=.3, suggestions=["repair"],
                                           repair_tasks=[task(task_id="stable", category=category)])
    state["score_history"] = {sid: [.3, .3]}
    nodes = PipelineNodes(_deps())
    first = nodes.revise(state)
    assert first["route_hint"] == HINT_RETRY
    state.update(first)
    state["attempts"][sid] = 2
    escalation = nodes.revise(state)
    assert escalation["route_hint"] == HINT_RETRY
    state.update(escalation)
    assert REPAIR_STRATEGIES[category] in _collect_feedback(state, sid)
    assert "labels independently readable" in _collect_feedback(state, sid)
    progress = state["repair_progress"][sid]
    assert progress["stable"]["escalated_at"] == 3
    repeated = nodes.revise(state)
    assert repeated["repair_progress"][sid] == progress
    state["attempts"][sid] = 3
    handoff = nodes.revise(state)
    assert handoff["route_hint"] == HINT_HUMAN
    payload = json.loads(handoff["events"][0]["payload_json"])
    assert payload["repair_handoff"]["unresolved_tasks"][0]["task_id"] == "stable"
    assert payload["repair_handoff"]["suggested_strategies"]["stable"] == REPAIR_STRATEGIES[category]
    assert payload["repair_metrics"]["attempt"] == 3


def test_partial_progress_is_bounded_and_does_not_mutate_state() -> None:
    feedback = CriticFeedback(passed=False, suggestions=["repair"], repair_tasks=[task()])
    original: dict[str, Any] = {}
    first, _, _ = update_repair_progress(feedback, original, 1, 6)
    feedback.repair_tasks[0].status = "partial"
    second, escalations, _ = update_repair_progress(feedback, first, 2, 6)
    assert second["r1-01"]["failures"] == 0 and not escalations
    third, _, _ = update_repair_progress(feedback, second, 3, 6)
    fourth, escalations, _ = update_repair_progress(feedback, third, 4, 6)
    assert escalations and fourth["r1-01"]["escalated_at"] == 5
    assert original == {} and first["r1-01"]["failures"] == 1


def test_budget_never_extends_for_escalation() -> None:
    state = _state()
    sid = state["shots"][0].shot_id
    state["attempts"][sid] = 2
    state["max_attempts_per_shot"] = 2
    state["feedback"][sid] = CriticFeedback(passed=False, suggestions=["repair"], repair_tasks=[task()])
    assert PipelineNodes(_deps()).revise(state)["route_hint"] == HINT_HUMAN
