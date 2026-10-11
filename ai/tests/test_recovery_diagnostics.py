import ast

import pytest

from scidirector_ai.diagnostics import error_evidence
from scidirector_ai.generation_quality import normalize_manim_waits, check_manim_structure
from scidirector_ai.config import Settings
from scidirector_ai.sandbox.manim import ManimSandbox, ManimSandboxError
from scidirector_ai.sandbox.runner import ExecResult
from test_coder import make_agent, make_shot, VALID_MANIM
from scidirector_ai.schemas import StyleGuide


def test_zero_wait_is_removed_without_another_model_call():
    code = VALID_MANIM + "\n        self.wait(duration=0.0)\n"
    agent = make_agent([code])
    result = agent.generate(shot=make_shot(), style_guide=StyleGuide())
    assert result.policy_ok and result.llm_attempts == 1
    assert "duration=0.0" not in result.code
    ast.parse(result.code)


@pytest.mark.parametrize("call", ["self.wait(0, stop_condition=effect())", "self.wait(t)",
                                  "other.wait(0)", "self.play(FadeIn(x), run_time=0)"])
def test_normalization_does_not_guess_or_drop_arguments(call):
    code = "if True:\n    " + call
    assert normalize_manim_waits(code) == code


def test_zero_wait_keeps_empty_blocks_valid_and_negative_durations_fail():
    assert ast.parse(normalize_manim_waits("if True:\n    self.wait(0)"))
    report = check_manim_structure(VALID_MANIM + "\n        self.wait(-1)\n")
    assert not report.ok and "时长必须大于 0" in report.violations[-1].reason


def test_rich_traceback_final_cause_survives_every_bounded_layer(tmp_path):
    output = "│ scene.py in wait │\n" * 180 + "\x1b[31mValueError: duration <= 0 seconds\x1b[0m\nManim cannot render this."
    result = ExecResult(command=["manim"], returncode=1, stdout="", stderr=output, duration_sec=1)
    with pytest.raises(ManimSandboxError) as error:
        ManimSandbox(Settings(_env_file=None, env="test"))._raise_if_failed(result, work=tmp_path)
    assert "duration <= 0" in str(error.value)[:300]
    assert "duration <= 0" in error.value.detail[:300]
    assert "duration <= 0" in error_evidence(str(error.value), error.value.detail, 800)
    assert len(error_evidence("failed", output, 800)) <= 800


def test_manim_rebuild_cannot_reinject_bad_spatial_baseline():
    agent=make_agent([VALID_MANIM])
    shot=make_shot().model_copy(update={'meta':{'repair_strategy':'rebuild'}})
    old=VALID_MANIM+'\n        label=Text("meaningful fact").move_to([99,99,0])'
    result=agent.generate(shot=shot,style_guide=StyleGuide(),previous_code=old)
    prompt=agent.llm.calls[0]['user']
    assert result.policy_ok and 'meaningful fact' in prompt
    assert '99,99' not in prompt and 'move_to([99' not in prompt
    assert '上一版代码（以此为基础修改）' not in prompt
