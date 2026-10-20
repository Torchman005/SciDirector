import json
from types import SimpleNamespace

import pytest

from scidirector_ai.agents.coder import CodeArtifact, CodeGenerationResult
from scidirector_ai.config import Settings
from scidirector_ai.llm import LLMError, Task
from scidirector_ai.narration_repair import (NarrationEdit, NarrationPlan, NarrationVerification,
    NarrationRepairError, repair_narration)
from scidirector_ai.pbconv import shot_from_pb, shot_to_pb, feedback_from_pb, feedback_to_pb
from scidirector_ai.schemas import CriticFeedback, RepairTask, RenderArtifact, SceneTag, ShotSpec, StyleGuide
from scidirector_ai.service import PipelineService, RenderFailed

OLD = "SwiGLU 是一种更平滑的激活函数。这里的数字只是示意，不代表实测。"
BEFORE = "SwiGLU 是一种更平滑的激活函数"
AFTER = "SwiGLU 是门控结构，一路经 Swish 后与另一路逐元素相乘"


def feedback(scope="narration"):
    return CriticFeedback(suggestions=["修正旁白定义"], repair_tasks=[RepairTask(
        task_id="science-7-02", category="logic", severity="blocking", start_sec=0, end_sec=7.4,
        target="narration", evidence=BEFORE, instruction=AFTER,
        acceptance="说明双分支逐元素门控，不再称为单一平滑曲线", repair_scope=scope)])


class Editor:
    def __init__(self, plan=None, verdict="correct"):
        self.plan = plan or NarrationPlan(verdict="revise", reason="门控结构不是单一激活曲线",
            edits=[NarrationEdit(task_id="science-7-02", before=BEFORE, after=AFTER)])
        self.verdict = verdict
        self.calls = []
        self.usage = SimpleNamespace(total_tokens=0)

    def chat_json(self, system, user, model, **kwargs):
        self.calls.append((system, json.loads(user), kwargs))
        assert kwargs["max_parse_attempts"] == 1
        if kwargs["task"] == Task.NARRATION_REPAIR:
            return self.plan
        assert kwargs["task"] == Task.NARRATION_VERIFY
        return NarrationVerification(verdict=self.verdict, reason="当前文本说明双分支门控，保留了示意数据限定")


def test_minimal_edit_preserves_facts_history_ledger_and_proto():
    original = ShotSpec(shot_id="s", narration=OLD, visual_brief="错误曲线", beats=["旧曲线"],
        keywords=["旧类比"], meta={"overlay_text": "旧标题", "other": "keep"})
    old_feedback = feedback("")  # The screenshot's legacy narration target remains actionable.
    llm = Editor()
    updated = repair_narration(llm, original, old_feedback, attempt=8)
    assert updated.narration == OLD.replace(BEFORE, AFTER)
    assert original.narration == OLD and not original.narration_revisions
    assert old_feedback.repair_tasks[0].status == "open"
    assert not updated.beats and not updated.keywords and "overlay_text" not in updated.meta
    assert updated.meta["other"] == "keep" and updated.meta["repair_strategy"] == "rebuild"
    assert updated.narration_revisions[0].before == OLD
    assert shot_from_pb(shot_to_pb(updated)) == updated
    assert feedback_from_pb(feedback_to_pb(feedback())) == feedback()
    assert len(llm.calls) == 2 and llm.calls[1][1]["corrected_narration"] == updated.narration
    assert repair_narration(llm, updated, old_feedback, attempt=9) == updated
    assert len(llm.calls) == 2  # Pending visual acceptance must not trigger another rewrite.


def test_visual_defect_does_not_rewrite_correct_narration_or_call_models():
    llm = Editor()
    shot = ShotSpec(narration=OLD)
    assert repair_narration(llm, shot, feedback("visual"), attempt=8) is shot
    assert not llm.calls


@pytest.mark.parametrize("edits", [
    [dict(task_id="foreign", before=BEFORE, after=AFTER)],
    [dict(task_id="science-7-02", before="不存在的原文", after=AFTER)],
    [dict(task_id="science-7-02", before=BEFORE, after=" ")],
    [dict(task_id="science-7-02", before=BEFORE, after=BEFORE)],
    [dict(task_id="science-7-02", before=BEFORE, after=AFTER),
     dict(task_id="science-7-02", before="更平滑的激活函数", after="门控结构")],
])
def test_ungrounded_or_overlapping_edits_fail_before_verification(edits):
    llm = Editor(NarrationPlan(verdict="revise", reason="test", edits=edits))
    with pytest.raises(NarrationRepairError):
        repair_narration(llm, ShotSpec(narration=OLD), feedback(), attempt=8)
    assert len(llm.calls) == 1


@pytest.mark.parametrize("verdict", ["incorrect", "uncertain"])
def test_independent_rejection_never_mutates_original(verdict):
    shot = ShotSpec(narration=OLD)
    with pytest.raises(NarrationRepairError, match="独立核对未通过"):
        repair_narration(Editor(verdict=verdict), shot, feedback(), attempt=8)
    assert shot.narration == OLD and not shot.narration_revisions


def test_partial_script_edits_stop_before_any_llm_or_render():
    llm = Editor()
    with pytest.raises(NarrationRepairError, match="整镜重做"):
        repair_narration(llm, ShotSpec(narration=OLD), feedback(), attempt=8, partial=True)
    assert not llm.calls


def test_editor_outage_is_bounded():
    def fail(*args, **kwargs):
        raise LLMError("timeout")
    with pytest.raises(NarrationRepairError, match="不可用"):
        repair_narration(SimpleNamespace(chat_json=fail), ShotSpec(narration=OLD), feedback(), attempt=8)


def test_linked_revision_synthesizes_corrected_words_and_fresh_marks(tmp_path, monkeypatch):
    from pathlib import Path
    import scidirector_ai.service as module
    from scidirector_ai.tts.base import SynthesisResult, SentenceMark, read_marks_sidecar, TTSError
    result = SimpleNamespace(video_path=str(tmp_path/"video.mp4"), duration_sec=5, width=1280,
        height=720, fps=24, engine="motion", render_cost_sec=1, partial_range_honored=False)
    monkeypatch.setattr(module,"build_renderer",lambda *args:SimpleNamespace(render=lambda *args:result))
    monkeypatch.setattr(module,"extract_frames",lambda *args,**kwargs:[])
    captured = []
    class Voice:
        name = "fixture"
        def synthesize(self, text, *, out_path, **kwargs):
            captured.append(text)
            out_path.parent.mkdir(parents=True,exist_ok=True)
            out_path.write_bytes(b"new voice")
            return SynthesisResult(out_path,5,(SentenceMark(text,.1,4.8),),"fixture")
    monkeypatch.setattr(module,"build_tts_provider",lambda *args:Voice())
    old_voice = tmp_path/"old.mp3"
    old_voice.write_bytes(b"old voice")
    service = PipelineService(Settings(_env_file=None,llm_provider="mock",tts_provider="none"),Editor())
    service._coder = SimpleNamespace(generate=lambda **kwargs:SimpleNamespace(policy_ok=True,code="new",
        artifact=SimpleNamespace(language="html+js"),overlay_text=""))
    service._critic = SimpleNamespace(review=lambda **kwargs:SimpleNamespace(feedback=CriticFeedback(passed=True),degraded=False))
    def run():
        return service.revise_shot(job_id="j",shot=ShotSpec(narration=OLD,tag=SceneTag.MOTION),human_comment="",
            attempt=8,style_guide=StyleGuide(),previous_feedback=feedback(),output_dir=str(tmp_path),
            previous_artifact=RenderArtifact(video_path="old.mp4",audio_path=str(old_voice)))
    updated, artifact, _, _ = run()
    assert captured == [updated.narration] and captured[0] != OLD
    assert artifact.audio_path != str(old_voice) and old_voice.read_bytes() == b"old voice"
    assert read_marks_sidecar(artifact.audio_path)[0].text == updated.narration
    assert Path(artifact.audio_path).read_bytes() == b"new voice"
    def fail(*args,**kwargs):
        raise TTSError("unavailable",retryable=False)
    monkeypatch.setattr(Voice,"synthesize",fail)
    with pytest.raises(RenderFailed,match="配音失败") as failed:
        run()
    assert not failed.value.retryable
    assert old_voice.read_bytes() == b"old voice"


def test_transient_audio_failure_can_retry_same_code_without_rewriting_script(monkeypatch):
    llm = Editor()
    service = PipelineService(Settings(_env_file=None,llm_provider="mock",tts_provider="none"), llm)
    service._coder = SimpleNamespace(generate=lambda **kwargs:SimpleNamespace(policy_ok=True,code="same new code",
        artifact=SimpleNamespace(language="html+js"),overlay_text=""))
    service._critic = SimpleNamespace(review=lambda **kwargs:SimpleNamespace(feedback=CriticFeedback(passed=True),degraded=False))
    calls = []
    def render(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RenderFailed("重做配音失败：temporary timeout")
        return RenderArtifact(video_path="fresh.mp4",audio_path="fresh.mp3"), False
    monkeypatch.setattr(service,"_render_shot",render)
    updated, _, result, _ = service.revise_shot(job_id="j",shot=ShotSpec(narration=OLD,code="old"),
        human_comment="",attempt=8,style_guide=StyleGuide(),previous_feedback=feedback())
    assert result.passed and updated.narration != OLD and len(calls) == 2
    assert len(llm.calls) == 2 and len(updated.narration_revisions) == 1


def test_graph_revised_marks_failure_is_not_a_silent_audio_fallback(tmp_path, monkeypatch):
    from scidirector_ai.graph import nodes as module
    from scidirector_ai.tts.base import SynthesisResult
    from test_retry_efficiency import _deps
    shot = repair_narration(Editor(),ShotSpec(narration=OLD),feedback(),attempt=8)
    deps = _deps()
    deps.tts = SimpleNamespace(name="fixture")
    monkeypatch.setattr(module,"synthesize_with_retry",lambda *args,**kwargs:SynthesisResult(tmp_path/"fresh.mp3",4))
    def fail(*args,**kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr(module,"write_marks_sidecar",fail)
    audio, error = module.PipelineNodes(deps)._synthesize_narration(shot,tmp_path)
    assert not audio and "时间戳写入失败" in error


def test_revision_redoes_audio_when_a_later_review_discovers_script_error(monkeypatch):
    service = PipelineService(Settings(_env_file=None, llm_provider="mock", tts_provider="none"), Editor())
    calls, renders, reviews = [], [], []
    def generate(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(policy_ok=True, code="new-code", artifact=SimpleNamespace(language="python"), overlay_text="")
    def render(**kwargs):
        renders.append(kwargs)
        return RenderArtifact(artifact_id=str(len(renders)),video_path=f"v{len(renders)}.mp4",audio_path=f"voice{len(renders)}.mp3"), False
    def review(**kwargs):
        reviews.append(kwargs)
        return SimpleNamespace(feedback=feedback() if len(reviews) == 1 else CriticFeedback(passed=True), degraded=False)
    service._coder = SimpleNamespace(generate=generate)
    service._critic = SimpleNamespace(review=review)
    monkeypatch.setattr(service, "_render_shot", render)
    monkeypatch.setattr("scidirector_ai.service.precheck_repairs", lambda *args: [])
    updated, artifact, result, _ = service.revise_shot(job_id="j",shot=ShotSpec(narration=OLD,code="old"),
        human_comment="", attempt=8, style_guide=StyleGuide())
    assert result.passed and len(calls) == 2 and artifact.audio_path == "voice2.mp3"
    assert renders[0]["shot"].narration == OLD and renders[1]["shot"].narration == updated.narration
    assert renders[1]["speech_path"] == ""  # Old voice and old marks must not survive a script edit.
    assert reviews[1]["shot"].narration == updated.narration != OLD
    assert calls[1]["shot"].meta["repair_strategy"] == "rebuild"


def test_failed_verification_does_not_generate_or_render(monkeypatch):
    service = PipelineService(Settings(_env_file=None,llm_provider="mock",tts_provider="none"), Editor(verdict="uncertain"))
    def unexpected(**kwargs):
        pytest.fail("must not generate or render an unverified rewrite")
    service._coder = SimpleNamespace(generate=unexpected)
    monkeypatch.setattr(service, "_render_shot", unexpected)
    with pytest.raises(RenderFailed, match="需人工核对"):
        service.revise_shot(job_id="j",shot=ShotSpec(narration=OLD),human_comment="",attempt=8,
            style_guide=StyleGuide(),previous_feedback=feedback())


def test_graph_passes_corrected_text_to_coder_and_has_human_exit(monkeypatch):
    from scidirector_ai.graph.nodes import PipelineNodes, HINT_RENDER, HINT_HUMAN, route_after_code
    from scidirector_ai.graph.state import NODE_ADVANCE
    from test_retry_efficiency import _deps, _state
    state = _state()
    shot = state["shots"][0].model_copy(update={"narration": OLD})
    state["shots"] = [shot]
    state["feedback"] = {shot.shot_id: feedback()}
    captured = []
    def generate(**kwargs):
        captured.append(kwargs)
        return CodeGenerationResult(artifact=CodeArtifact(code="old code",language="python"),policy_ok=True)
    deps = _deps(coder=SimpleNamespace(generate=generate))
    deps.llm = Editor()
    nodes = PipelineNodes(deps)
    update = nodes.code(state)
    assert update["route_hint"] == HINT_RENDER
    assert captured[0]["shot"].narration == update["shots"][0].narration != OLD
    # Semantic fields are committed with a rendered artifact, not before rendering succeeds.
    assert "narration" not in json.loads(update["events"][-1]["payload_json"])["patch"]
    deps.llm = Editor(verdict="uncertain")
    blocked = nodes.code(state)
    assert blocked["route_hint"] == HINT_HUMAN and route_after_code(blocked) == NODE_ADVANCE
    assert not blocked.get("shots") and len(captured) == 1


@pytest.mark.parametrize("tts_error", ["", "provider timeout"])
def test_graph_publishes_semantic_patch_only_with_new_audio_or_no_voice_config(tmp_path, monkeypatch, tts_error):
    from scidirector_ai.graph import nodes as module
    from test_retry_efficiency import _deps, _state
    state = _state()
    shot = repair_narration(Editor(),state["shots"][0].model_copy(update={"narration": OLD}),feedback(),attempt=2)
    state.update(shots=[shot],feedback={},attempts={shot.shot_id: 2})
    result = SimpleNamespace(video_path="new.mp4",duration_sec=5,width=320,height=240,fps=10,engine="manim",render_cost_sec=.1)
    deps = _deps(renderer=SimpleNamespace(render=lambda *args:result))
    deps.settings.sandbox_work_dir = str(tmp_path)
    monkeypatch.setattr(module,"extract_frames_with_times",lambda *args,**kwargs:[])
    nodes = module.PipelineNodes(deps)
    monkeypatch.setattr(nodes,"_synthesize_narration",lambda *args:("fresh.mp3" if not tts_error else "",tts_error))
    update = nodes.render(state)
    if tts_error:
        assert update["route_hint"] == module.HINT_HUMAN and "artifacts" not in update
        assert all(not e.get("artifact") for e in update["events"])
    else:
        from scidirector_ai.pbconv import event_to_pb
        patch = event_to_pb(update["events"][0]).narration_patch
        assert patch.narration == shot.narration
        assert patch.narration_revisions[0].before == OLD
        assert update["artifacts"][shot.shot_id].audio_path == "fresh.mp3"
