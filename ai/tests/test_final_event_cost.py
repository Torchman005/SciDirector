"""收尾事件的成本负载（Python -> Go 的成本数据通道）。

**为什么这组测试必须存在，而且必须用字面量断言键名。**
``payload_json`` 是两侧唯一没有 proto 约束的通道：键名写错不会有任何编译错误，
也不会让任何一侧的单测变红 —— 它只表现为「Go 侧的成本永远是 0」。
Go 侧有一份对称的测试（``backend/internal/pbconv/cost_json_test.go``）用照抄
本文件产出格式的字面量断言解析；两侧各自钉住自己那一半，改动任何一侧都会变红。

与 ``shots_payload_json`` 同理：字段名用 snake_case、值全是数字。

``summary`` 与 ``cost`` 并存是刻意的：``summary`` 是已有消费方可能在看的结构，
不因为新增 ``cost`` 就删掉 —— 契约只增不改，改动要有理由。
"""

from __future__ import annotations

import json

from scidirector_ai.graph.builder import _final_event
from scidirector_ai.llm import Usage


class _FakeLLM:
    """只需要 ``usage`` 的替身：``_final_event`` 除用量外不碰 LLM 的任何能力。"""

    def __init__(self, prompt: int, completion: int, calls: int) -> None:
        self.usage = Usage(
            prompt_tokens=prompt, completion_tokens=completion, calls=calls
        )


def _payload(llm) -> dict:
    ev = _final_event("job-cost-test", llm)
    return json.loads(ev["payload_json"])


def test_final_event_cost_uses_documented_key_names() -> None:
    """键名是跨语言契约，逐字面量钉住。"""
    payload = _payload(_FakeLLM(100, 250, 3))

    assert "cost" in payload, "Go 侧的成本核算依赖 payload_json 里的 cost 对象"
    cost = payload["cost"]
    # 键名逐个断言而不是比较整个字典：多一个键不该让用例失败，
    # 但**少一个或拼错**必须失败。
    assert cost["llm_prompt_tokens"] == 100
    assert cost["llm_completion_tokens"] == 250
    assert cost["llm_total_tokens"] == 350  # 派生自前两者，不是独立字段
    assert cost["llm_calls"] == 3


def test_final_event_cost_values_are_plain_numbers() -> None:
    """必须是 JSON 数字而不是字符串：Go 侧反序列化进 int，字符串会直接报错。"""
    cost = _payload(_FakeLLM(12, 34, 1))["cost"]
    for key, value in cost.items():
        assert isinstance(value, int) and not isinstance(value, bool), f"{key} 不是整数：{value!r}"


def test_final_event_keeps_summary_for_existing_consumers() -> None:
    """``summary`` 不能被 ``cost`` 取代 —— 已有消费方可能在看它。"""
    payload = _payload(_FakeLLM(100, 250, 3))

    assert payload["summary"] == {"total_tokens": 350, "calls": 3}


def test_final_event_cost_is_zero_when_nothing_was_called() -> None:
    """真的没调用过时如实报 0，而不是省掉这个对象。

    区分「报了 0」与「根本没报」对下游有意义：Go 侧前者会落一条用量记录，
    后者让 ``llm_usage`` 保持 null。mock 模式走的就是这条路
    （``_mock_response`` 不经过 usage 累加），真实任务的成本因此恒为 0。
    """
    payload = _payload(_FakeLLM(0, 0, 0))

    assert payload["cost"] == {
        "llm_prompt_tokens": 0,
        "llm_completion_tokens": 0,
        "llm_total_tokens": 0,
        "llm_calls": 0,
    }
    assert payload["summary"] == {"total_tokens": 0, "calls": 0}
