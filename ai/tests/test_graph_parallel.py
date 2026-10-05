"""Parallel shot graph contracts without external renderers or temp directories."""

from __future__ import annotations

import time
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver

from scidirector_ai.graph import builder
from scidirector_ai.graph.state import initial_state, make_event
from scidirector_ai.graph.state import merge_motion_reports
from scidirector_ai.graph.nodes import _motion_feedback
from scidirector_ai.media import MotionReport
from scidirector_ai.schemas import CriticFeedback, SceneTag, ShotSpec, StyleGuide


def _shots() -> list[ShotSpec]:
    return [
        ShotSpec(shot_id=f"job-p-s{i:03d}", index=i, narration=f"part {i}",
                 visual_brief="figure", tag=SceneTag.MATH, duration_sec=4)
        for i in range(2)
    ]


def _state() -> dict[str, Any]:
    return initial_state(
        job_id="job-p", raw_script="a script", style_guide=StyleGuide(),
        target_duration_sec=8, max_attempts_per_shot=2, locale="zh-CN",
    )


class FakeNodes:
    plan_calls = 0
    code_calls: list[tuple[str, int]] = []
    fail_once = False
    reject_first = False

    def __init__(self, deps: Any) -> None:
        pass

    def plan(self, state: dict[str, Any]) -> dict[str, Any]:
        type(self).plan_calls += 1
        shots = _shots()
        return {"shots": shots, "attempts": {s.shot_id: 0 for s in shots},
                "events": [make_event({**state, "shots": shots}, node="plan", message="planned")],
                "route_hint": "next"}

    def code(self, state: dict[str, Any]) -> dict[str, Any]:
        shot = state["shots"][state["cursor"]]
        attempt = state["attempts"].get(shot.shot_id, 0) + 1
        if type(self).fail_once and shot.index == 1:
            type(self).fail_once = False
            raise RuntimeError("transient worker failure")
        type(self).code_calls.append((shot.shot_id, attempt))
        time.sleep(0.2)
        shots = list(state["shots"])
        shots[shot.index] = shot.model_copy(update={"code": f"code-{shot.index}-{attempt}"})
        return {"shots": shots, "attempts": {shot.shot_id: attempt},
                "route_hint": "render", "events": [make_event(state, node="code", message="coded",
                                                               shot=shot, attempt=attempt)]}

    def render(self, state: dict[str, Any]) -> dict[str, Any]:
        shot = state["shots"][state["cursor"]]
        time.sleep(0.2)
        return {"route_hint": "critique", "events": [make_event(state, node="render",
                    message="rendered", shot=shot, attempt=state["attempts"][shot.shot_id])]}

    def critique(self, state: dict[str, Any]) -> dict[str, Any]:
        shot = state["shots"][state["cursor"]]
        attempt = state["attempts"][shot.shot_id]
        passed = not (type(self).reject_first and shot.index == 0 and attempt == 1)
        feedback = CriticFeedback(passed=passed, score=0.9 if passed else 0.4,
                                  suggestions=[] if passed else ["change figure"])
        return {"feedback": {shot.shot_id: feedback}, "route_hint": "ok" if passed else "retry",
                "events": [make_event(state, node="critique", message="reviewed", shot=shot,
                                      attempt=attempt, status="APPROVED" if passed else "REJECTED")]}

    def revise(self, state: dict[str, Any]) -> dict[str, Any]:
        shot = state["shots"][state["cursor"]]
        attempt = state["attempts"][shot.shot_id]
        retry = attempt < state["max_attempts_per_shot"]
        return {"route_hint": "retry" if retry else "human",
                "events": [make_event(state, node="revise", message="retry" if retry else "human",
                                      shot=shot, attempt=attempt,
                                      status="RETRYING" if retry else "AWAITING_HUMAN")]}

    def advance(self, state: dict[str, Any]) -> dict[str, Any]:
        cursor = state["cursor"] + 1
        return {"cursor": cursor, "finished": cursor >= len(state["shots"]),
                "route_hint": "done" if cursor >= len(state["shots"]) else "next"}


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeNodes.plan_calls = 0
    FakeNodes.code_calls = []
    FakeNodes.fail_once = False
    FakeNodes.reject_first = False
    monkeypatch.setattr(builder, "PipelineNodes", FakeNodes)


def _run(app: Any, *, thread: str, concurrency: int = 4, value: Any = None) -> tuple[list[dict[str, Any]], Any]:
    config = {"configurable": {"thread_id": thread}, "max_concurrency": concurrency,
              "recursion_limit": 100}
    events: list[dict[str, Any]] = []
    for mode, chunk in app.stream(_state() if value is None else value, config=config,
                                  stream_mode=["updates", "custom"]):
        if mode == "custom":
            events.append(chunk)
        elif mode == "updates":
            for node, update in chunk.items():
                if node != "shot" and isinstance(update, dict):
                    events.extend(update.get("events") or [])
    return events, app.get_state(config).values


def test_parallel_results_events_and_timing() -> None:
    app = builder.build_parallel_graph(None, MemorySaver())  # type: ignore[arg-type]
    start = time.monotonic()
    events, state = _run(app, thread="parallel", concurrency=4)
    parallel_sec = time.monotonic() - start
    serial_app = builder.build_graph(None, MemorySaver())  # type: ignore[arg-type]
    start = time.monotonic()
    serial_config = {"configurable": {"thread_id": "serial"}, "recursion_limit": 100}
    list(serial_app.stream(_state(), config=serial_config))
    serial_state = serial_app.get_state(serial_config).values
    serial_sec = time.monotonic() - start

    assert parallel_sec < serial_sec * 0.85, (parallel_sec, serial_sec)
    assert [s.code for s in state["shots"]] == ["code-0-1", "code-1-1"]
    assert [s.code for s in serial_state["shots"]] == ["code-0-1", "code-1-1"]
    assert set(state["feedback"]) == {s.shot_id for s in state["shots"]}
    assert {e["shot_id"] for e in events if e["node"] == "code"} == set(state["feedback"])
    assert len([e for e in events if e["node"] == "code"]) == 2
    assert state["finished"] is True


@pytest.mark.parametrize("max_attempts, expected", [(2, "APPROVED"), (1, "AWAITING_HUMAN")])
def test_retries_and_circuit_breaker(max_attempts: int, expected: str) -> None:
    FakeNodes.reject_first = True
    state = _state()
    state["max_attempts_per_shot"] = max_attempts
    app = builder.build_parallel_graph(None, MemorySaver())  # type: ignore[arg-type]
    config = {"configurable": {"thread_id": f"retry-{max_attempts}"},
              "max_concurrency": 4, "recursion_limit": 100}
    events = []
    for mode, chunk in app.stream(state, config=config, stream_mode=["updates", "custom"]):
        if mode == "custom":
            events.append(chunk)
    result = app.get_state(config).values
    first = result["shots"][0].shot_id
    assert result["attempts"][first] == max_attempts
    assert expected in [e["status"] for e in events if e["shot_id"] == first]
    assert result["attempts"][result["shots"][1].shot_id] == 1


def test_resume_after_failed_shot_does_not_replan() -> None:
    FakeNodes.fail_once = True
    app = builder.build_parallel_graph(None, MemorySaver())  # type: ignore[arg-type]
    config = {"configurable": {"thread_id": "resume"}, "max_concurrency": 4,
              "recursion_limit": 100}
    with pytest.raises(RuntimeError, match="transient worker failure"):
        list(app.stream(_state(), config=config))
    assert FakeNodes.plan_calls == 1
    assert app.get_state(config).next
    list(app.stream(None, config=config))
    assert FakeNodes.plan_calls == 1
    assert app.get_state(config).values["finished"] is True
    assert FakeNodes.code_calls.count(("job-p-s000", 1)) == 1
    assert FakeNodes.code_calls.count(("job-p-s001", 1)) == 1


def test_motion_feedback_names_affected_beat_and_can_clear_old_report() -> None:
    shot = _shots()[0].model_copy(update={"beats": ["opening", "figure moves", "ending"]})
    report = MotionReport(ratios=[0.1, 0.0, 0.1], static_spans=[(1.5, 2.5, 0.0)],
                          min_change_ratio=0.005)
    assert "第 2 段「figure moves」" in _motion_feedback(report, shot)
    assert merge_motion_reports({shot.shot_id: "stale"}, {shot.shot_id: None}) == {}
