"""豆包（火山引擎）TTS 适配器的请求头测试。

这一条针对的是一个**没有报错、只有含糊失败码**的坑：同一个端点靠
`Resource-Id` 请求头区分"开通了哪个产品"，而声音复刻出的 speaker_id
只能在大模型合成那条线上使用。少了这个头，服务端只会回一句
「未开通/无权限」，不会告诉你少了个头 —— 因此必须由测试钉住它真的被发出去了。
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from scidirector_ai.tts.doubao import DoubaoTTSProvider

#: 一段极短的假音频（内容无所谓，只需要是合法 base64）。
FAKE_AUDIO_B64 = base64.b64encode(b"fake-mp3-bytes").decode()


class _FakeResponse:
    status_code = 200

    def __init__(self, body: dict) -> None:
        self._body = body

    def json(self) -> dict:
        return self._body

    @property
    def text(self) -> str:
        return ""


def _provider(**kw) -> DoubaoTTSProvider:
    base = dict(appid="app-id", access_token="tok", cluster="volcano_tts")
    base.update(kw)
    return DoubaoTTSProvider(**base)


def _capture(monkeypatch) -> dict:
    """把 httpx.post 换成记录器，返回它捕获到的那次调用。"""
    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002 - 对齐真实签名
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResponse({"code": 3000, "data": FAKE_AUDIO_B64})

    import scidirector_ai.tts.doubao as mod

    monkeypatch.setattr(mod.httpx, "post", fake_post)
    return captured


def test_resource_id_header_is_sent_when_configured(monkeypatch, tmp_path: Path) -> None:
    captured = _capture(monkeypatch)
    provider = _provider(resource_id="volc.service_type.10029")

    provider.synthesize("你好", out_path=tmp_path / "a.mp3")

    assert captured["headers"]["Resource-Id"] == "volc.service_type.10029"


def test_resource_id_header_is_omitted_when_not_configured(monkeypatch, tmp_path: Path) -> None:
    """留空必须**不带**这个头：保持与既有（未验证的）行为一致。

    多发一个空值头可能被服务端当成"指定了一个空的资源"，从而让原本能用的
    老账号突然失败 —— 那会是一次"改配置功能却弄坏了老路径"。
    """
    captured = _capture(monkeypatch)
    provider = _provider()

    provider.synthesize("你好", out_path=tmp_path / "a.mp3")

    assert "Resource-Id" not in captured["headers"]


def test_authorization_uses_semicolon_not_space(monkeypatch, tmp_path: Path) -> None:
    # 火山引擎的鉴权头是 `Bearer;<token>`（分号）。写成空格会得到鉴权失败，
    # 而错误信息通常不会点明这一点 —— 属于典型的"看代码看不出来"的坑。
    captured = _capture(monkeypatch)
    provider = _provider()

    provider.synthesize("你好", out_path=tmp_path / "a.mp3")

    assert captured["headers"]["Authorization"] == "Bearer;tok"


def test_cloned_voice_id_is_used_as_voice_type(monkeypatch, tmp_path: Path) -> None:
    """复刻出的 speaker_id 就是 voice_type —— 这是"配置声音复刻"的落点。"""
    captured = _capture(monkeypatch)
    provider = _provider()

    provider.synthesize("你好", out_path=tmp_path / "a.mp3", voice="S_abc123")

    assert captured["json"]["audio"]["voice_type"] == "S_abc123"


def test_failure_code_is_surfaced_with_server_message(monkeypatch, tmp_path: Path) -> None:
    """失败时必须把服务端的 code/message 原样带出来。

    排「未开通 / 鉴权失败 / 音色不存在」全靠这两个字段；
    退化成「解析失败」会让排查完全无从下手。
    """

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002
        return _FakeResponse({"code": 3001, "message": "resource not granted"})

    import scidirector_ai.tts.doubao as mod

    monkeypatch.setattr(mod.httpx, "post", fake_post)
    provider = _provider()

    with pytest.raises(Exception) as exc:
        provider.synthesize("你好", out_path=tmp_path / "a.mp3")
    assert "3001" in str(exc.value)
    assert "resource not granted" in str(exc.value)
