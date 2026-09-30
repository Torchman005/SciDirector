"""豆包 TTS 两代调用方式的测试。

用户指出「豆包语音使用新版调用方式：使用 X-Api-Key」。新版与老版**不只是换个
请求头**：请求体字段与响应体形态都不同（新版是分块/SSE）。拿一套头去打另一代
的端点，得到的只是一句含糊的「鉴权失败/未开通」—— 所以代次是显式配置，
而这些差异必须有测试钉住。

本文件专测新增的那一半（api_key 鉴权 / v3 请求体 / 分块响应）；
老版那一半在 test_doubao_tts.py。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from scidirector_ai.tts.doubao import (
    AUTH_MODE_API_KEY,
    AUTH_MODE_BEARER,
    API_STYLE_V1,
    API_STYLE_V3,
    DoubaoTTSProvider,
)


def provider(**kw) -> DoubaoTTSProvider:
    base = dict(appid="app", access_token="tok", api_key="key-123")
    base.update(kw)
    return DoubaoTTSProvider(**base)


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------


def test_api_key_mode_sends_x_api_key_and_no_bearer() -> None:
    headers = provider(auth_mode=AUTH_MODE_API_KEY).build_headers()
    assert headers["X-Api-Key"] == "key-123"
    assert "Authorization" not in headers, "新版不该再带老版的鉴权头"


def test_bearer_mode_still_works_and_sends_no_api_key() -> None:
    headers = provider(auth_mode=AUTH_MODE_BEARER).build_headers()
    assert headers["Authorization"] == "Bearer;tok"
    assert "X-Api-Key" not in headers


def test_resource_id_header_name_depends_on_generation() -> None:
    # 老版叫 Resource-Id、新版叫 X-Api-Resource-Id。写错名字等于没带。
    old = provider(auth_mode=AUTH_MODE_BEARER, resource_id="volc.service_type.10029").build_headers()
    new = provider(auth_mode=AUTH_MODE_API_KEY, resource_id="volc.service_type.10029").build_headers()
    assert old["Resource-Id"] == "volc.service_type.10029"
    assert new["X-Api-Resource-Id"] == "volc.service_type.10029"
    assert "Resource-Id" not in new


def test_api_key_mode_needs_only_the_key() -> None:
    """新版最省事的一点：不必再凑 appid + access token 两个值。"""
    ok, reason = provider(auth_mode=AUTH_MODE_API_KEY, appid="", access_token="").available()
    assert ok, reason
    ok, reason = provider(auth_mode=AUTH_MODE_API_KEY, api_key="").available()
    assert not ok and "API_KEY" in reason


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


def test_v3_payload_uses_req_params_and_speaker() -> None:
    payload = provider(api_style=API_STYLE_V3).build_payload("你好", "S_abc", None)
    assert "req_params" in payload and "user" in payload
    assert payload["req_params"]["speaker"] == "S_abc"
    assert payload["req_params"]["text"] == "你好"
    # V1 的字段不该出现在 V3 里。
    assert "app" not in payload and "audio" not in payload


def test_v3_speed_is_a_percent_offset_not_a_multiplier() -> None:
    # V1 用倍率（speed_ratio=1.5），V3 用整百比偏移（speech_rate=50）。
    # 混用会让语速离谱 —— 1.5 当偏移量就是「快 1.5%」，几乎听不出来；
    # 反过来 50 当倍率就是 50 倍速。
    p = provider(api_style=API_STYLE_V3)
    assert p.build_payload("x", "v", 1.0)["req_params"]["audio_params"]["speech_rate"] == 0
    assert p.build_payload("x", "v", 1.5)["req_params"]["audio_params"]["speech_rate"] == 50
    assert p.build_payload("x", "v", 0.8)["req_params"]["audio_params"]["speech_rate"] == -20


def test_v1_payload_unchanged() -> None:
    payload = provider(api_style=API_STYLE_V1).build_payload("你好", "v", 1.5)
    assert payload["audio"]["voice_type"] == "v"
    assert payload["audio"]["speed_ratio"] == 1.5


# ---------------------------------------------------------------------------
# 响应解析：这是新增逻辑里最容易写错的一块
# ---------------------------------------------------------------------------


def test_parses_single_json_v1_body() -> None:
    body = json.dumps({"code": 3000, "data": base64.b64encode(b"mp3-bytes").decode()})
    code, _msg, audio = provider()._parse_response_body(body)
    assert code == 3000
    assert audio == b"mp3-bytes"


def test_parses_chunked_v3_body_by_decoding_each_chunk() -> None:
    """分块响应必须**逐片解码**再拼接。

    这条用一个 2 字节的首片来证明：它的 base64 带 `=` 补齐。若实现是把 base64
    字符串接起来再解码一次，`=` 出现在中间会让解码直接失败（或得到乱码）——
    这正是最容易写错的地方。
    """
    c1 = base64.b64encode(b"ab").decode()  # YWI=   <- 中间那个 = 是陷阱
    c2 = base64.b64encode(b"cdef").decode()
    assert c1.endswith("="), "构造前提：首片必须带补齐符"

    body = "\n".join(
        [
            json.dumps({"code": 0, "data": c1}),
            json.dumps({"code": 0, "data": c2}),
        ]
    )
    code, _msg, audio = provider(api_style=API_STYLE_V3)._parse_response_body(body)
    assert code == 0
    assert audio == b"abcdef"


def test_parses_sse_prefixed_chunks_and_ignores_noise() -> None:
    body = "\n".join(
        [
            ": heartbeat",
            "data: " + json.dumps({"code": 0, "data": base64.b64encode(b"xy").decode()}),
            "",
            "data: [DONE]",
            "not json at all",
        ]
    )
    _code, _msg, audio = provider(api_style=API_STYLE_V3)._parse_response_body(body)
    assert audio == b"xy"


def test_v3_success_code_is_zero_while_v1_is_3000() -> None:
    v3 = provider(api_style=API_STYLE_V3)
    assert v3._is_success(0)
    assert not v3._is_success(3000)
    v1 = provider(api_style=API_STYLE_V1)
    assert v1._is_success(3000)
    assert not v1._is_success(0)


def test_missing_code_is_treated_as_success_so_audio_decides() -> None:
    # V3 的分块里可能只在某一片给 code、也可能不给；
    # 此时以"有没有音频"为准，而不是一律判失败。
    assert provider(api_style=API_STYLE_V3)._is_success(None)


def test_v3_uses_v3_endpoint_when_none_configured(monkeypatch, tmp_path: Path) -> None:
    """没显式配端点时，新版必须打新版端点 —— 拿老端点配新头必然失败。"""
    captured: dict = {}

    class _Resp:
        status_code = 200
        text = json.dumps({"code": 0, "data": base64.b64encode(b"audio").decode()})

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002
        captured["url"] = url
        captured["headers"] = headers
        return _Resp()

    import scidirector_ai.tts.doubao as mod

    monkeypatch.setattr(mod.httpx, "post", fake_post)
    p = provider(api_style=API_STYLE_V3, auth_mode=AUTH_MODE_API_KEY, endpoint=mod.DEFAULT_ENDPOINT)
    p.synthesize("你好", out_path=tmp_path / "a.mp3")

    assert captured["url"] == mod.DEFAULT_V3_ENDPOINT
    assert captured["headers"]["X-Api-Key"] == "key-123"


def test_empty_audio_reports_the_server_message(monkeypatch, tmp_path: Path) -> None:
    """没有音频时必须把 code/message/响应片段带出来。

    "鉴权过了但没声音"是最难猜的一类失败：不看服务端原话就无从判断
    是音色不存在、参数不对，还是产品线没开通。
    """

    class _Resp:
        status_code = 200
        text = json.dumps({"code": 0, "message": "speaker not found", "data": ""})

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002
        return _Resp()

    import scidirector_ai.tts.doubao as mod

    monkeypatch.setattr(mod.httpx, "post", fake_post)
    p = provider(api_style=API_STYLE_V3, auth_mode=AUTH_MODE_API_KEY)

    with pytest.raises(Exception) as exc:
        p.synthesize("你好", out_path=tmp_path / "a.mp3")
    assert "speaker not found" in str(exc.value)
