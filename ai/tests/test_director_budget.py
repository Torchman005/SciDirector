"""Director requests have their own bounded budget; no real model calls."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scidirector_ai.agents import director
from scidirector_ai.agents.base import load_prompt
from scidirector_ai.config import Settings
from scidirector_ai.llm import LLMClient, LLMError, LLMTruncatedError
from scidirector_ai.schemas import StyleGuide


def plan_json() -> str:
    return json.dumps({'outline': '输入经处理变为输出。', 'shots': [
        {'narration': '输入进入处理器。', 'visual_brief': '输入沿路径进入处理器。',
         'duration_sec': 4, 'tag': 'MOTION', 'beats': ['输入出现', '进入处理器']},
        {'narration': '处理后得到输出。', 'visual_brief': '输出从处理器另一侧展开。',
         'duration_sec': 4, 'tag': 'MOTION', 'beats': ['处理器变化', '输出展开']},
    ]}, ensure_ascii=False)


def model_client(monkeypatch: pytest.MonkeyPatch, responses: list[tuple[str, str]],
                 *, cap: int = 8192) -> tuple[LLMClient, list[dict]]:
    settings = Settings(_env_file=None, env='test', llm_provider='mock', llm_max_tokens=2048,
                        llm_max_retries=3, director_plan_max_tokens=cap)
    monkeypatch.setattr(director, 'get_settings', lambda: settings)
    client = LLMClient(settings)
    client._mock = False
    requests: list[dict] = []

    def create(**body):
        requests.append(body)
        content, reason = responses[len(requests) - 1]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=reason)],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=300),
        )

    client._text_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return client, requests


def run_plan(client: LLMClient):
    return director.DirectorAgent(client).plan(job_id='test-director-budget',
        raw_script='输入进入处理器。处理后得到输出。', style_guide=StyleGuide(), target_duration_sec=8)


def test_director_first_call_uses_own_budget_and_preserves_order(monkeypatch):
    client, requests = model_client(monkeypatch, [(plan_json(), 'stop')])
    plan = run_plan(client)
    assert len(requests) == 1 and requests[0]['max_tokens'] == 4096
    assert [s.narration for s in plan.shots] == ['输入进入处理器。', '处理后得到输出。']
    assert [s.index for s in plan.shots] == [0, 1]
    assert sum(s.duration_sec for s in plan.shots) == 8
    assert client.settings.llm_max_tokens == 2048


@pytest.mark.parametrize('cap', [2048, 4096, 8192, 16384, 32768])
def test_director_expands_only_confirmed_length_within_existing_retry(monkeypatch, cap):
    client, requests = model_client(monkeypatch, [(plan_json()[:-2], 'length'), (plan_json(), 'stop')], cap=cap)
    plan = run_plan(client)
    assert len(plan.shots) == 2
    assert [r['max_tokens'] for r in requests] == [min(4096, cap), cap]
    retry = requests[1]['messages'][1]['content']
    assert 'finish_reason=length' in retry and '保留完整旁白与分镜顺序' in retry
    assert '减少冗余关键帧' not in retry
    assert plan.total_tokens == 800 and client.usage.calls == 2


@pytest.mark.parametrize('content', ['{"shots":', '{"shots":"not an array"}'])
def test_normal_json_or_schema_error_does_not_expand_budget(monkeypatch, content):
    client, requests = model_client(monkeypatch, [(content, 'stop'), (plan_json(), 'stop')])
    assert len(run_plan(client).shots) == 2
    assert [r['max_tokens'] for r in requests] == [4096, 4096]


def test_two_truncations_fail_without_accepting_partial_or_valid_looking_json(monkeypatch):
    client, requests = model_client(monkeypatch, [(plan_json(), 'length')] * 2)
    with pytest.raises(LLMError, match='本次调用已尝试 2 次') as error:
        run_plan(client)
    assert [r['max_tokens'] for r in requests] == [4096, 8192]
    assert isinstance(error.value.__cause__, LLMTruncatedError)
    assert error.value.__cause__.raw_response == plan_json()
    assert client.usage.calls == 2  # no extra transport retry or third planning call


def test_prompt_keeps_plan_concise_without_discarding_narration():
    text = load_prompt('director')
    assert '不逐镜重复画风原文' in text and '不要为了节省输出而漏掉后半段' in text
    assert '不输出 Canvas/Remotion 代码' in text and 'index` 由系统' in text


def test_director_budget_loads_from_environment_and_is_public(monkeypatch):
    monkeypatch.setenv('SCID_DIRECTOR_PLAN_MAX_TOKENS', '24000')
    settings = Settings(_env_file=None, env='test', llm_provider='mock')
    assert settings.director_plan_max_tokens == 24000
    assert settings.public_summary()['director_plan_max_tokens'] == 24000


@pytest.mark.parametrize('cap', [255, 128001])
def test_director_budget_rejects_invalid_configuration(cap):
    with pytest.raises(ValueError, match='director_plan_max_tokens'):
        Settings(_env_file=None, director_plan_max_tokens=cap)
