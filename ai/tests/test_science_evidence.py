import json
from types import SimpleNamespace

import pytest

from scidirector_ai.science_evidence import manim_review_manifest


def test_mislabeled_curve_retains_actual_math_and_repair_targets_without_execution():
    code = '''
from manim import *
raise RuntimeError("must never execute")
class SciShotScene(Scene):
    def construct(self):
        swiglu = ax.plot(lambda x: 1.1*(np.tanh(x)+1)/2*1.6)
        label = Text("SwiGLU", font_size=36).next_to(ax, DOWN)
'''
    evidence = manim_review_manifest(code)
    payload = json.loads(evidence[evidence.index('{'):])
    assert payload['literal_text'][0]['target']=='label'
    assert payload['literal_text'][0]['literal_text']==['SwiGLU']
    assert payload['mathematical_source'][0]['target']=='swiglu'
    assert 'np.tanh(x)' in payload['mathematical_source'][0]['expression']
    assert 'blocking/logic' in evidence


def test_helper_functions_and_formulas_remain_data_with_bounded_valid_json():
    source='def relu(x):\n    return max(0,x)\n'
    source+='\n'.join(f't{i}=MathTex("x"*1000)' for i in range(80))
    source+='\ng=ax.plot(relu)'
    evidence=manim_review_manifest(source)
    payload=json.loads(evidence[evidence.index('{'):])
    assert payload['helper_definitions'][0]['target']=='relu'
    assert len(evidence)<13000
    assert manim_review_manifest('<html>')==''


def test_mathematical_error_cannot_be_overridden_by_vlm_and_repeated_source_is_cached(tmp_path):
    from scidirector_ai.science_audit import ScienceAudit, ScienceFinding
    from scidirector_ai.agents.critic import CriticAgent
    from scidirector_ai.config import Settings
    from scidirector_ai.schemas import ShotSpec, SceneTag, StyleGuide
    from test_critic_agent import make_artifact
    calls=[]
    def audit(*args,**kwargs):
        calls.append(kwargs)
        return ScienceAudit(findings=[ScienceFinding(verdict='incorrect',target='swiglu',evidence='tanh 与门控乘积定义不同',
            instruction='改为正确门控机制',acceptance='体现两个线性分支和逐元素乘法')])
    def vision(*args,**kwargs):raise AssertionError('科学错误未修复，不应浪费视觉调用或被其放行')
    agent=CriticAgent(SimpleNamespace(chat_json=audit,vision_json=vision),Settings(_env_file=None,llm_provider='mock'))
    shot=ShotSpec(tag=SceneTag.MATH,code='swiglu=ax.plot(lambda x:np.tanh(x))\nlabel=Text("SwiGLU")')
    for attempt in [1,2]:
        result=agent.review(shot=shot,artifact=make_artifact(tmp_path),style_guide=StyleGuide(),attempt=attempt)
        assert not result.feedback.passed and not result.degraded
        assert result.feedback.repair_tasks[0].actionable
    assert len(calls)==1 and calls[0]['max_parse_attempts']==1


def test_only_explicit_source_proof_closes_science_tasks_and_empty_audit_is_invalid():
    from scidirector_ai.science_audit import ScienceAudit, ScienceFinding, ScienceResolution, resolve_science, science_tasks
    from scidirector_ai.schemas import CriticFeedback
    wrong=ScienceFinding(verdict='incorrect',target='f',evidence='wrong definition',instruction='fix',acceptance='correct definition')
    audit=ScienceAudit(findings=[wrong])
    task=science_tasks(audit,attempt=1,duration=5)[0]
    previous=CriticFeedback(passed=False,suggestions=['fix'],repair_tasks=[task])
    correct=ScienceFinding(verdict='correct',target='f',evidence='formula now matches definition')
    assert resolve_science(previous,ScienceAudit(findings=[correct])).repair_tasks[0].status=='open'
    proof=ScienceResolution(task_id=task.task_id,resolved=True,evidence='actual corrected expression')
    resolved=resolve_science(previous,ScienceAudit(findings=[correct],resolutions=[proof]))
    assert resolved.repair_tasks[0].status=='resolved'
    from scidirector_ai.recovery import preserved_science
    assert 'correct definition' in preserved_science(resolved)
    assert 'actual corrected expression' in preserved_science(resolved)
    assert resolve_science(previous,ScienceAudit(findings=[wrong],resolutions=[proof])).repair_tasks[0].status=='open'
    with pytest.raises(ValueError):ScienceAudit(findings=[])


def test_reaudit_keeps_acceptance_but_excludes_stale_claims_and_director_draft():
    from scidirector_ai.science_audit import science_request
    from scidirector_ai.schemas import ShotSpec, SceneTag, CriticFeedback, RepairTask
    shot=ShotSpec(tag=SceneTag.MATH,visual_brief='旧错误的 S 形草案',
        code='curve=ax.plot(lambda x:x*x/(1+np.exp(-x)))\nlabel=Text("W=V=1,b=c=0 切片")')
    old=CriticFeedback(passed=False,suggestions=['旧代码缺少参数'],repair_tasks=[
        RepairTask(task_id='science-1-01',category='logic',severity='blocking',target='curve',
            evidence='旧代码缺少参数',instruction='旧修法',acceptance='须明确参数和切片含义',repair_scope='both')])
    request=science_request(shot,old)
    data=json.loads(request)
    assert '旧错误' not in request and '旧代码' not in request and '旧修法' not in request
    assert data['previous_science_tasks']==[{'task_id':'science-1-01','target':'curve','acceptance':'须明确参数和切片含义','repair_scope':'both'}]
    assert 'W=V=1,b=c=0' in request


def test_visual_reaudit_uses_current_science_and_acceptance_not_stale_diagnosis(tmp_path):
    from scidirector_ai.agents.critic import CriticAgent, _RawCritique
    from scidirector_ai.science_audit import ScienceAudit, ScienceFinding
    from scidirector_ai.config import Settings
    from scidirector_ai.schemas import ShotSpec, SceneTag, StyleGuide, CriticFeedback, RepairTask
    from test_critic_agent import make_artifact
    captured=[]
    def vision(system,user,*args,**kw):
        captured.append(user)
        return _RawCritique(passed=True,score=.9,logic_score=.9,readability_score=.9,pacing_score=.9,aesthetics_score=.9)
    def audit(*args,**kw):
        return ScienceAudit(findings=[ScienceFinding(verdict='correct',target='curve',evidence='当前公式正确')])
    old=CriticFeedback(passed=False,suggestions=['STALE_TRANSCRIPT'],repair_tasks=[
        RepairTask(task_id='r1-01',category='readability',severity='major',target='label',
            evidence='STALE_OBSERVATION',instruction='STALE_IMPLEMENTATION',acceptance='标签须完整可见',frame_indices=[1])])
    agent=CriticAgent(SimpleNamespace(chat_json=audit,vision_json=vision),Settings(_env_file=None,llm_provider='mock'))
    result=agent.review(shot=ShotSpec(tag=SceneTag.MATH,code='eq=MathTex("x=1")'),artifact=make_artifact(tmp_path),
        style_guide=StyleGuide(),attempt=2,previous_review=old,previous_feedback='STALE_TRANSCRIPT',user_intent='想先展示公式')
    assert captured and '当前公式正确' in captured[0] and '标签须完整可见' in captured[0]
    assert '想先展示公式' in captured[0] and 'STALE_' not in captured[0]
    assert not result.feedback.passed  # Dropping stale prose never discards the obligation.


@pytest.mark.parametrize('corrected_pass', [True, False])
def test_reasoning_dispute_gets_one_visual_recheck_and_cannot_override_it(tmp_path,corrected_pass):
    from scidirector_ai.agents.critic import CriticAgent, _RawCritique
    from scidirector_ai.science_audit import ScienceAudit, ScienceFinding
    from scidirector_ai.review_reasoning import ReviewReasonCheck, ReviewDispute
    from scidirector_ai.config import Settings
    from scidirector_ai.llm import Task
    from scidirector_ai.schemas import ShotSpec, SceneTag, StyleGuide, RepairTask
    from test_critic_agent import make_artifact
    calls=[]
    def audit(*args,**kw):
        calls.append(kw['task'])
        if kw['task']==Task.SCIENCE:
            return ScienceAudit(findings=[ScienceFinding(verdict='correct',target='eq',evidence='定义正确')])
        assert kw['max_parse_attempts']==1
        return ReviewReasonCheck(disputes=[ReviewDispute(claim='未并排所以逻辑错误',correction='先后展示也可以比较')])
    def vision(system,user,*args,**kw):
        calls.append(Task.CRITIQUE)
        if calls.count(Task.CRITIQUE)==2:
            assert '先后展示也可以比较' in user and '不是要求通过' in user
            if corrected_pass:
                return _RawCritique(passed=True,score=.9,logic_score=.9,readability_score=.9,pacing_score=.9,aesthetics_score=.9)
        return _RawCritique(passed=False,score=.4,logic_score=.3,readability_score=.8,pacing_score=.8,aesthetics_score=.8,
            issues=['未并排所以逻辑错误'],suggestions=['检查比较'],repair_tasks=[RepairTask(
                task_id='r1-01',category='logic',severity='blocking',target='eq',frame_indices=[1],
                evidence='未并排所以逻辑错误',instruction='检查比较',acceptance='比较内容科学正确')])
    agent=CriticAgent(SimpleNamespace(chat_json=audit,vision_json=vision),Settings(_env_file=None,llm_provider='mock'))
    result=agent.review(shot=ShotSpec(tag=SceneTag.MATH,code='eq=MathTex("x=1")'),artifact=make_artifact(tmp_path),
        style_guide=StyleGuide(),attempt=1)
    assert result.feedback.passed is corrected_pass
    assert calls==[Task.SCIENCE,Task.CRITIQUE,Task.REVIEW_REASONING,Task.CRITIQUE]
