"""Fish Audio TTS 的防呆测试。

针对一个真实踩过的坑：切换 TTS 服务商时 `SCID_TTS_VOICE` 里往往还留着
上一家的音色名（如豆包的 `custom_zh_clone_agent`），而它在 fish.py 里
**优先于** `SCID_FISH_REFERENCE_ID`。请求于是带着别家的音色名发出去，
服务端只回一个与"音色配错了"毫无关系的错误，排查方向直接跑偏。

因此这里先验形状（32 位十六进制）再发请求。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scidirector_ai.tts.fish import FishAudioTTSProvider

GOOD_REF = "0dcdcfacd3934bb799c38498b507e5c5"


def provider(**kw) -> FishAudioTTSProvider:
    base = dict(api_key="k")
    base.update(kw)
    return FishAudioTTSProvider(**base)


def test_rejects_foreign_voice_name_before_calling_api(monkeypatch, tmp_path: Path) -> None:
    """别的服务商的音色名必须**在发请求之前**被拦下。"""
    called = {"n": 0}

    def fake_post(*a, **kw):  # noqa: ANN002, ANN003
        called["n"] += 1
        raise AssertionError("不该发出请求：形状校验应当先拦下")

    import scidirector_ai.tts.fish as mod

    monkeypatch.setattr(mod.httpx, "post", fake_post)
    p = provider(reference_id="")

    # 模拟"SCID_TTS_VOICE 里留着豆包音色"的情形：voice 参数优先。
    with pytest.raises(Exception) as exc:
        p.synthesize("你好", out_path=tmp_path / "a.mp3", voice="custom_zh_clone_agent")

    assert called["n"] == 0, "形状不对时不该发请求"
    msg = str(exc.value)
    assert "32 位十六进制" in msg
    assert "SCID_TTS_VOICE" in msg, "错误信息必须指出真正的来源，否则用户无从下手"


def test_accepts_valid_reference_id(monkeypatch, tmp_path: Path) -> None:
    seen: dict = {}

    class _Resp:
        status_code = 200
        content = b"ID3fake-mp3"

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002
        seen["json"] = json
        seen["headers"] = headers
        return _Resp()

    import scidirector_ai.tts.fish as mod

    monkeypatch.setattr(mod.httpx, "post", fake_post)
    p = provider(reference_id=GOOD_REF, model="s2-pro")
    p.synthesize("你好", out_path=tmp_path / "a.mp3")

    assert seen["json"]["reference_id"] == GOOD_REF
    # model 走**请求头**（官方示例如此），不是 body。
    assert seen["headers"]["model"] == "s2-pro"
    assert seen["headers"]["Authorization"] == "Bearer k"


def test_no_reference_id_is_allowed(monkeypatch, tmp_path: Path) -> None:
    """不给音色时用账号默认音色 —— 这是合法用法，不该被拦。"""

    class _Resp:
        status_code = 200
        content = b"ID3fake-mp3"

    import scidirector_ai.tts.fish as mod

    monkeypatch.setattr(mod.httpx, "post", lambda *a, **kw: _Resp())
    p = provider()
    p.synthesize("你好", out_path=tmp_path / "a.mp3")
