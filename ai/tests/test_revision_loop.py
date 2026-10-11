from pathlib import Path
from types import SimpleNamespace

import pytest

from scidirector_ai.config import Settings
from scidirector_ai.schemas import CriticFeedback, RepairTask, RenderArtifact, SceneTag, ShotSpec, StyleGuide
from scidirector_ai.service import PipelineService, RenderFailed, _artifact_samples
from scidirector_ai.agents.coder import CoderAgent
from test_scene import Checker, Stub, compile_it, spec


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
    assert calls[-1]['shot'].meta['repair_strategy'] == 'rebuild'


def test_empty_comment_uses_system_error_and_review_and_preserves_failed_source(tmp_path, monkeypatch):
    calls=[]
    svc = PipelineService(Settings(_env_file=None,llm_provider='mock',tts_provider='none'))
    def generate(**kw):
        calls.append(kw)
        return SimpleNamespace(policy_ok=True, code='broken' if len(calls)==1 else 'fixed',
                               artifact=SimpleNamespace(language='python'), overlay_text='')
    def render(**kw):
        if kw['code']=='broken':
            raise RenderFailed('ValueError: duration <= 0')
        return RenderArtifact(video_path='fixed.mp4'),False
    svc._coder=SimpleNamespace(generate=generate)
    svc._critic=SimpleNamespace(review=lambda **kw:SimpleNamespace(feedback=feedback(True),degraded=False))
    monkeypatch.setattr(svc,'_render_shot',render)
    review=feedback()
    review.repair_tasks.append(RepairTask(task_id='science-1-01',category='logic',severity='blocking',
        start_sec=0,end_sec=5,target='curve',evidence='错误函数',instruction='明确切片条件',acceptance='显示固定参数',
        status='resolved',resolution_evidence='W=V=1,b=c=0'))
    result=svc.revise_shot(job_id='test',shot=ShotSpec(tag=SceneTag.MATH,code='original',
        visual_brief='错误的单变量曲线草案',beats=['错误的旧节拍']),
        human_comment='',attempt=12,style_guide=StyleGuide(),previous_error='wait(0.0) failed',previous_feedback=review)
    assert result[2].passed and len(calls)==2
    assert 'wait(0.0)' in calls[0]['feedback_text'] and 'r2-01' in calls[0]['feedback_text']
    assert calls[1]['previous_code']=='broken'
    assert calls[1]['shot'].meta['repair_strategy']=='rebuild'
    assert 'ValueError' in calls[1]['feedback_text']
    assert '显示固定参数' in calls[1]['feedback_text'] and 'W=V=1,b=c=0' in calls[1]['feedback_text']
    assert calls[1]['shot'].visual_brief!='错误的单变量曲线草案' and not calls[1]['shot'].beats


def test_nonretryable_environment_failure_stops_without_burning_three_rounds(tmp_path, monkeypatch):
    svc=PipelineService(Settings(_env_file=None,llm_provider='mock',tts_provider='none'))
    calls=[]
    def generate(**kw):
        calls.append(kw)
        return SimpleNamespace(policy_ok=True,code='new',artifact=SimpleNamespace(language='python'),overlay_text='')
    svc._coder=SimpleNamespace(generate=generate)
    def render(**kw):raise RenderFailed('缺少浏览器',retryable=False)
    monkeypatch.setattr(svc,'_render_shot',render)
    with pytest.raises(RenderFailed, match='第 1/3 轮停止.*缺少浏览器'):
        svc.revise_shot(job_id='test',shot=ShotSpec(tag=SceneTag.MATH),human_comment='',attempt=8,style_guide=StyleGuide())
    assert len(calls)==1


def test_rebuild_uses_full_schema_even_with_corrupt_previous_html():
    llm=Stub([spec(text='新结构')])
    coder=CoderAgent(llm,Settings(_env_file=None,llm_provider='mock'),quality_checker=Checker())
    shot=ShotSpec(tag=SceneTag.MOTION,meta={'repair_strategy':'rebuild'})
    result=coder.generate(shot=shot,style_guide=StyleGuide(),previous_code='<html>corrupt</html>')
    assert result.policy_ok
    from scidirector_ai.scene import extract_scene
    assert extract_scene(result.code).elements[0].text=='新结构'


def test_missing_frame_does_not_shift_evidence_time():
    samples=_artifact_samples(RenderArtifact(duration_sec=4,frame_samples=['frame_00.png','frame_03.png']),4)
    assert samples[0].ts==.2 and samples[1].ts==2.5


def test_exact_edit_still_renders_and_reviews_and_invalid_edit_never_falls_back(monkeypatch):
    settings=Settings(_env_file=None,llm_provider='mock',tts_provider='none')
    service=PipelineService(settings)
    llm=Stub([])
    service._coder=CoderAgent(llm,settings,quality_checker=Checker())
    rendered=[]; reviewed=[]
    def render(**kw):
        rendered.append(kw)
        return RenderArtifact(video_path='edited.mp4'),False
    def review(**kw):
        reviewed.append(kw)
        return SimpleNamespace(feedback=feedback(True),degraded=False)
    monkeypatch.setattr(service,'_render_shot',render)
    service._critic=SimpleNamespace(review=review)
    shot=ShotSpec(tag=SceneTag.MOTION,code=compile_it(spec()))
    from scidirector_ai.scene_revision import EXACT_REVISION_PREFIX, SceneRevision
    patch=SceneRevision(upsert=[spec(text='精确修改').elements[0]])
    def run(comment):
        return service.revise_shot(job_id='test',shot=shot,human_comment=comment,
            attempt=2,style_guide=StyleGuide())
    assert run(EXACT_REVISION_PREFIX+patch.model_dump_json())[2].passed
    assert len(rendered)==len(reviewed)==1 and not llm.calls
    for invalid in ['{','{"remove":["unknown"]}','{}']:
        with pytest.raises(ValueError):run(EXACT_REVISION_PREFIX+invalid)
    assert len(rendered)==len(reviewed)==1 and not llm.calls
