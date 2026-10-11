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
