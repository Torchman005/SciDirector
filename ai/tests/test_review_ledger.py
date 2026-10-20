"""An outage is not a failed content review and must never erase repair evidence."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from scidirector_ai.agents.critic import CriticAgent, _normalize_repair_tasks
from scidirector_ai.config import Settings
from scidirector_ai.llm import LLMError
from scidirector_ai.pbconv import feedback_from_pb, feedback_to_pb
from scidirector_ai.schemas import CriticFeedback, FeedbackSource, RenderArtifact, RepairEvidence, SceneTag, ShotSpec, StyleGuide
from scidirector_ai.service import PipelineService
from test_repair_tasks import task


def ledger() -> CriticFeedback:
    return CriticFeedback(kind="content_review", score=.6, attempt=7, artifact_id="before-v7",
        suggestions=["separate labels"], repair_tasks=[task(task_id="open"),
            task(task_id="partial", status="partial", resolution_evidence="first label separated"),
            task(task_id="done", status="resolved", resolution_evidence="readable in current frame")])


@pytest.mark.parametrize("frames", [[], ["a.png", "b.png"]])
def test_review_outage_preserves_all_original_tasks_and_statuses(frames):
    previous = ledger()
    original = previous.model_dump()
    def unavailable(*args, **kwargs):
        raise LLMError("provider unavailable")
    reviewer = CriticAgent(SimpleNamespace(vision_json=unavailable),
                          Settings(_env_file=None, llm_provider="mock"))
    result = reviewer.review(shot=ShotSpec(tag=SceneTag.MOTION),
        artifact=RenderArtifact(artifact_id="after-v8", duration_sec=5, frame_samples=frames),
        style_guide=StyleGuide(), attempt=8, previous_review=previous)
    assert result.degraded and not result.passed
    assert result.feedback.kind == "review_unavailable" and result.feedback.message
    assert result.feedback.artifact_id == "after-v8"
    assert result.feedback.repair_tasks == previous.repair_tasks
    assert previous.model_dump() == original


def test_proto_roundtrip_keeps_unscored_fault_and_comparison_versions():
    previous = ledger()
    previous.repair_tasks[0].evidence_pairs = [RepairEvidence(ts=1.2, before="old.png", after="new.png",
        before_artifact_id="v7", after_artifact_id="v8")]
    # Revalidation matches the checkpoint loading boundary.
    previous = CriticFeedback.model_validate(previous.model_dump())
    back = feedback_from_pb(feedback_to_pb(previous))
    assert back == previous
    fault = CriticFeedback(kind="review_unavailable", source=FeedbackSource.SYSTEM,
        message="No frames", handoff_reason="Review unavailable", repair_tasks=previous.repair_tasks)
    assert feedback_from_pb(feedback_to_pb(fault)) == fault
    assert not fault.suggestions


def test_model_cannot_supply_evidence_file_paths():
    fabricated = task(evidence_pairs=[dict(ts=1, before="secret.txt", after="another.txt")])
    assert not _normalize_repair_tasks([fabricated], attempt=1)[0].evidence_pairs


def test_degraded_graph_retains_new_pairs_and_excludes_outage_from_score_trend(tmp_path):
    from scidirector_ai.graph.nodes import HINT_HUMAN, PipelineNodes
    from test_retry_efficiency import _deps, _state

    before, after = tmp_path / "before.png", tmp_path / "after.png"
    before.write_bytes(b"before")
    after.write_bytes(b"after")
    def unavailable(*args, **kwargs):
        raise LLMError("VLM timeout")
    old = ledger()
    outcome = CriticAgent(SimpleNamespace(vision_json=unavailable),
        Settings(_env_file=None, llm_provider="mock")).review(
        shot=ShotSpec(tag=SceneTag.MOTION), artifact=RenderArtifact(artifact_id="v8",duration_sec=5,frame_samples=["frame.png"]),
        style_guide=StyleGuide(), attempt=8, previous_review=old,
        repair_prechecks=[dict(task_id="open", pairs=[dict(ts=1,before=str(before),after=str(after),
            before_artifact_id="actual-before",after_artifact_id="v8")])])
    state = _state()
    sid = state["shots"][0].shot_id
    state["feedback"] = {sid: old}
    state["artifacts"] = {sid: RenderArtifact(artifact_id="v8",duration_sec=5)}
    state["score_history"] = {sid: [.6]}
    result = PipelineNodes(_deps(critic=SimpleNamespace(review=lambda **kwargs: outcome))).critique(state)
    assert result["route_hint"] == HINT_HUMAN and result["score_history"][sid] == [.6]
    kept = result["feedback"][sid].repair_tasks[0]
    assert kept.status == "open" and kept.acceptance == old.repair_tasks[0].acceptance
    assert kept.evidence_pairs[0].before_artifact_id == "actual-before"
    assert kept.evidence_pairs[0].after == str(after)


def revision_service(tmp_path: Path, monkeypatch, *, degraded=False):
    service = PipelineService(Settings(_env_file=None, llm_provider="mock", tts_provider="none"))
    generations, reviews = [], []
    def generate(**kwargs):
        generations.append(kwargs)
        return SimpleNamespace(policy_ok=True, code=f"version-{len(generations)}",
            artifact=SimpleNamespace(language="html+js"), overlay_text="")
    def render(**kwargs):
        return RenderArtifact(artifact_id=f"v{kwargs['attempt']}", video_path=str(tmp_path / "clip.mp4")), False
    def review(**kwargs):
        reviews.append(kwargs)
        fb = CriticFeedback(kind="review_unavailable", source=FeedbackSource.SYSTEM, message="VLM timeout") if degraded else ledger()
        fb.score = .7 + len(reviews) * .1
        return SimpleNamespace(feedback=fb, degraded=degraded)
    service._coder = SimpleNamespace(generate=generate)
    service._critic = SimpleNamespace(review=review)
    monkeypatch.setattr(service, "_render_shot", render)
    monkeypatch.setattr("scidirector_ai.service.precheck_repairs", lambda *args: [])
    return service, generations, reviews


def test_single_shot_outage_keeps_tasks_for_next_request(tmp_path, monkeypatch):
    service, generations, _ = revision_service(tmp_path, monkeypatch, degraded=True)
    previous = ledger()
    result = service.revise_shot(job_id="j", shot=ShotSpec(tag=SceneTag.MOTION, code="old"),
        human_comment="", attempt=8, style_guide=StyleGuide(), previous_feedback=previous)
    assert len(generations) == 1
    assert result[2].kind == "review_unavailable" and result[2].repair_tasks == previous.repair_tasks


def test_two_unimproved_repairs_stop_even_if_scores_rise(tmp_path, monkeypatch):
    service, generations, reviews = revision_service(tmp_path, monkeypatch)
    result = service.revise_shot(job_id="j", shot=ShotSpec(tag=SceneTag.MOTION, code="old"),
        human_comment="", attempt=8, style_guide=StyleGuide(), previous_feedback=ledger())
    assert len(generations) == len(reviews) == 2
    assert not result[2].passed and "连续两轮" in result[2].handoff_reason
    assert result[2].repair_tasks[0].acceptance == ledger().repair_tasks[0].acceptance


def test_two_identical_evidence_rounds_keep_comparison_without_calling_vlm(tmp_path, monkeypatch):
    service, generations, reviews = revision_service(tmp_path, monkeypatch)
    pair = dict(ts=1, before="before.png", after="after.png")
    monkeypatch.setattr("scidirector_ai.service.precheck_repairs", lambda *args: [
        dict(task_id="open",status="unchanged",can_skip=True,pairs=[pair])])
    result = service.revise_shot(job_id="j",shot=ShotSpec(tag=SceneTag.MOTION,code="old"),human_comment="",
        attempt=8,style_guide=StyleGuide(),previous_feedback=ledger(),
        previous_artifact=RenderArtifact(artifact_id="v7",video_path="old.mp4"))
    assert len(generations) == 2 and not reviews
    assert result[2].kind == "review_unavailable" and "连续两轮" in result[2].handoff_reason
    assert result[2].repair_tasks[0].evidence_pairs[0].before_artifact_id == "v7"
