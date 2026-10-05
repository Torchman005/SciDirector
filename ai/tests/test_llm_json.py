"""Complete JSON is required; truncated scene responses must stay recognizable."""
from types import SimpleNamespace

import pytest

from scidirector_ai.config import Settings
from scidirector_ai.llm import LLMClient, LLMParseError, Task, extract_json
from scidirector_ai.scene import SceneSpec


@pytest.mark.parametrize("response", [
    '{"elements": [{"id":"title"}], "explanation": "unfinished',
    '{"elements": [{"id":"title"}],',
    '说明：{"elements": [{"id":"title"}], "explanation":',
])
def test_truncated_outer_object_cannot_be_replaced_with_inner_elements(response: str) -> None:
    with pytest.raises(LLMParseError) as error:
        extract_json(response)
    assert error.value.raw_response == response


@pytest.mark.parametrize("response", [
    '{"ok":true}',
    '```json\n{"ok":true}\n```',
    '结果：{"ok":true}。另一个说明 {ignore}',
])
def test_complete_json_survives_explanatory_wrappers(response: str) -> None:
    assert extract_json(response) == {"ok": True}


def test_truncated_completion_uses_parse_budget_and_keeps_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(env="test", llm_provider="mock", llm_max_retries=3)
    client = LLMClient(settings)
    client._mock = False
    requests = []
    content = '{"elements": [{"id":"title"}], "explanation": "unfinished'

    def create(**body):
        requests.append(body)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="length")],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=8192),
        )

    client._text_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(LLMParseError) as error:
        client.chat_json("schema", "scene", SceneSpec, task=Task.SCENE,
                         max_tokens=8192, max_parse_attempts=1)
    assert len(requests) == 1  # no hidden transport retries for a completed but truncated response
    assert "finish_reason=length" in str(error.value)
    assert "max_tokens=8192" in str(error.value)
    assert error.value.raw_response == content
    assert client.usage.calls == 1 and client.usage.completion_tokens == 8192


def test_length_response_can_recover_in_existing_parse_retry() -> None:
    settings = Settings(env="test", llm_provider="mock")
    client = LLMClient(settings)
    client._mock = False
    requests = []
    scene = '{"elements":[{"id":"title","kind":"text","text":"主题","box":{"x":0.1,"y":0.1,"width":0.8,"height":0.2}}]}'

    def create(**body):
        requests.append(body)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=scene if len(requests) > 1 else scene[:-2]),
                                     finish_reason="stop" if len(requests) > 1 else "length")],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=200),
        )

    client._text_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    parsed = client.chat_json("schema", "scene", SceneSpec, task=Task.SCENE)
    assert parsed.elements[0].text == "主题" and len(requests) == 2
    assert "finish_reason=length" in requests[1]["messages"][1]["content"]
    assert client.usage.calls == 2
