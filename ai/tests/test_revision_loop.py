from pathlib import Path
from types import SimpleNamespace

import pytest

from scidirector_ai.config import Settings
from scidirector_ai.schemas import CriticFeedback, RepairTask, RenderArtifact, SceneTag, ShotSpec, StyleGuide
from scidirector_ai.service import PipelineService, RenderFailed, _artifact_samples


def feedback(passed=False):
    return CriticFeedback(passed=passed, score=.9 if passed else .6, suggestions=[] if passed else ['修正连接方向'],
        repair_tasks=[] if passed else [RepairTask(task_id='r2-01', category='logic', frame_indices=[2],
            target='link', evidence='箭头方向错误', instruction='修正连接方向', acceptance='输入流向输出')])


def harness(tmp_path, monkeypatch, codes, outcomes):
    service=PipelineService(Settings(_env_file=None,llm_provider='mock',tts_provider='none',shot_max_attempts=3))
    calls=[]; renders=[]; reviews=[]
    def generate(**kw):
        calls.append(kw)
        value=next(codes)
        return SimpleNamespace(policy_ok=value is not None,policy_summary='布局冲突',code=value,
            artifact=SimpleNamespace(language='html+js'),overlay_text='')
    def render(**kw):
        renders.append(kw)
        folder=tmp_path/str(len(renders));folder.mkdir()
        video=folder/'clip.mp4';video.write_bytes(b'video')
        audio=folder/'voice.mp3';audio.write_bytes(b'audio')
        return RenderArtifact(video_path=str(video),audio_path=kw['speech_path'] or str(audio),
            duration_sec=4,frame_samples=[str(folder/'frame_00.png'),str(folder/'frame_01.png')]),False
    def review(**kw):
        reviews.append(kw)
        return SimpleNamespace(feedback=next(outcomes),degraded=False)
    service._coder=SimpleNamespace(generate=generate)
    service._critic=SimpleNamespace(review=review)
    monkeypatch.setattr(service,'_render_shot',render)
    monkeypatch.setattr('scidirector_ai.service.precheck_repairs',lambda *a:[{'task_id':'r2-01','status':'changed','pairs':[]}])
    def run():
        return service.revise_shot(job_id='test',shot=ShotSpec(tag=SceneTag.MOTION,code='original'),
            human_comment='改善动画',attempt=2,style_guide=StyleGuide())
    return run,calls,renders,reviews


def test_automatic_revision_reuses_speech_and_reviews_actual_previous_version(tmp_path,monkeypatch):
    run,calls,renders,reviews=harness(tmp_path,monkeypatch,iter(['first','fixed']),iter([feedback(),feedback(True)]))
    shot,artifact,fb,_=run()
    assert fb.passed and shot.code=='fixed'
    assert len(calls)==2 and 'r2-01' in calls[1]['feedback_text']
    assert calls[1]['previous_code']=='first'
    assert renders[1]['speech_path']==artifact.audio_path
    assert reviews[1]['previous_review'].repair_tasks[0].task_id=='r2-01'
    assert reviews[1]['repair_prechecks'][0]['status']=='changed'


def test_later_failures_keep_last_rendered_video_and_never_claim_pass(tmp_path,monkeypatch):
    run,calls,renders,reviews=harness(tmp_path,monkeypatch,iter(['first',None,None]),iter([feedback()]))
    shot,artifact,fb,_=run()
    assert shot.code=='first' and Path(artifact.video_path).is_file() and not fb.passed
    assert len(calls)==3 and len(reviews)==1 and len(renders)==1


def test_unchanged_code_has_bounded_retries_and_never_renders(tmp_path,monkeypatch):
    run,calls,renders,_=harness(tmp_path,monkeypatch,iter(['original']*3),iter([]))
    with pytest.raises(RenderFailed,match='未改变实际代码'):run()
    assert len(calls)==3 and not renders


def test_missing_frame_does_not_shift_evidence_time():
    samples=_artifact_samples(RenderArtifact(duration_sec=4,frame_samples=['frame_00.png','frame_03.png']),4)
    assert samples[0].ts==.2 and samples[1].ts==2.5
