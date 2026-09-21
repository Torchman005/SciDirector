"""多服务商支持（阶段五·DeepSeek 与阿里云百炼）。

## 这个文件要证明的三件事

1. **解析正确**：给定配置，文本/视觉各自的 provider、base_url、model、key
   都符合该服务商的约定（纯函数，无网络）。
2. **请求真的发对了**：用一个**本地的 OpenAI 兼容假服务端**接住请求，
   断言它收到的 `Authorization`、`model`、以及（视觉时）图片分片 ——
   只验证"配置对象里字段是对的"证明不了这些字段真的上了线。
3. **安全规则**：真文本 + 没配视觉时，**绝不**退回 mock 审查。
   mock 审查会返回伪造的"审查通过"，让未审查的画面进成片。

真实端点的部分（401 鉴权失败）单独放在最后，直接打 api.deepseek.com 与
dashscope.aliyuncs.com —— 本机实测这两家可达、而 api.openai.com 不通，
因此"供应商接线正确"这件事是**能**验证的，不该只用假服务端糊过去。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from scidirector_ai.config import Settings
from scidirector_ai.llm import LLMClient, LLMError
from scidirector_ai.providers import PROVIDERS, resolve_target

# ---------------------------------------------------------------------------
# 1) 解析（纯函数）
# ---------------------------------------------------------------------------


def test_each_provider_has_a_sane_default() -> None:
    """每家服务商的出厂默认必须是"能直接用的"。

    漏填 base_url 会让请求打到官方根域名（404 或返回非预期内容），
    而错误信息看起来像"模型不存在" —— 因此这里把 base_url 的存在性钉住。
    """
    for name, spec in PROVIDERS.items():
        if name == "mock":
            continue
        assert spec.base_url.startswith("https://"), f"{name} 的 base_url 不是 https"
        assert "/v1" in spec.base_url or "compatible-mode" in spec.base_url, (
            f"{name} 的 base_url 看起来不是 OpenAI 兼容端点：{spec.base_url}"
        )
        assert spec.api_key_env.startswith("SCID_"), f"{name} 的密钥变量名没有 SCID_ 前缀"
        assert spec.text_model, f"{name} 没有默认文本模型"


def test_deepseek_has_no_vision_and_says_so() -> None:
    """DeepSeek 没有视觉模型，必须**显式**表达。

    若给它留一个看起来能用的视觉默认值，结果是每个镜头都调用失败、
    由 Critic 降级转人工 —— 现象是"每一镜都进人工队列"，
    而原因看起来像"VLM 服务坏了"。这种模糊必须避免。
    """
    assert PROVIDERS["deepseek"].supports_vision is False
    t = resolve_target(kind="vision", provider="deepseek", model="", api_key="k")
    assert t.supports_vision is False
    assert t.problem, "没有视觉能力时必须给出原因"
    assert "SCID_VLM_PROVIDER" in t.problem, "原因里要指出下一步该怎么做"


def test_bailian_compatible_endpoint_is_not_the_bare_domain() -> None:
    """百炼必须用 /compatible-mode/v1。

    这是实测出来的：根域名返回 404，而兼容端点才认 OpenAI 协议。
    写成根域名会让每一次调用都失败。
    """
    spec = PROVIDERS["bailian"]
    assert spec.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert spec.vlm_model, "百炼应当有视觉模型（Critic 依赖它）"


def test_vision_provider_follows_text_by_default() -> None:
    s = Settings(llm_provider="bailian", dashscope_api_key="k")
    assert s.text_target().provider == "bailian"
    assert s.vision_target().provider == "bailian"


def test_vision_provider_can_differ_from_text() -> None:
    """**推荐组合**：DeepSeek 写代码（便宜）+ 百炼审画面（有视觉）。"""
    s = Settings(
        llm_provider="deepseek",
        deepseek_api_key="dk",
        vlm_provider="bailian",
        dashscope_api_key="bk",
    )
    text, vision = s.text_target(), s.vision_target()
    assert (text.provider, text.model) == ("deepseek", "deepseek-chat")
    assert (vision.provider, vision.model) == ("bailian", "qwen-vl-max")
    assert text.usable and vision.usable
    # 两家密钥不能串：用错密钥的表现是 401，而 401 看起来像"密钥过期了"。
    assert text.api_key == "dk" and vision.api_key == "bk"


def test_explicit_model_overrides_provider_default() -> None:
    s = Settings(llm_provider="bailian", dashscope_api_key="k", llm_model="qwen-max")
    assert s.text_target().model == "qwen-max"


def test_generic_overrides_win() -> None:
    """通用覆盖（自建网关场景）优先于两家各自的开关。"""
    s = Settings(
        llm_provider="deepseek",
        deepseek_api_key="dk",
        llm_api_key="shared",
        llm_base_url="http://gateway.internal/v1",
    )
    t = s.text_target()
    assert t.api_key == "shared"
    assert t.base_url == "http://gateway.internal/v1"


def test_legacy_openai_base_url_still_applies_to_openai_only() -> None:
    """`SCID_OPENAI_BASE_URL` 是旧专用开关：对 OpenAI 生效，不该泄漏到别家。"""
    s = Settings(llm_provider="openai", openai_api_key="k", openai_base_url="http://proxy/v1")
    assert s.text_target().base_url == "http://proxy/v1"

    s2 = Settings(llm_provider="deepseek", deepseek_api_key="k", openai_base_url="http://proxy/v1")
    assert s2.text_target().base_url == PROVIDERS["deepseek"].base_url


def test_unknown_provider_is_rejected_at_startup() -> None:
    """写错服务商名必须**启动即失败**，而不是悄悄退回 mock。

    悄悄退回 mock 会得到"看起来在用真模型、其实是占位内容"的结果 ——
    那正是本项目反复强调要避免的静默失败。
    """
    with pytest.raises(Exception):
        Settings(llm_provider="deepsek")  # 少一个 e


def test_missing_key_degrades_to_mock_with_a_reason() -> None:
    t = resolve_target(kind="text", provider="deepseek", model="", api_key="")
    assert t.is_mock is True
    assert "SCID_DEEPSEEK_API_KEY" in t.problem, "原因应指出该配哪个变量"


# ---------------------------------------------------------------------------
# 2) 请求真的发对了（本地 OpenAI 兼容假服务端）
# ---------------------------------------------------------------------------


class _FakeOpenAI:
    """最小 OpenAI 兼容服务端：记录收到的请求并回一个合法响应。"""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.auth: list[str] = []
        self._server: HTTPServer | None = None
        self.base_url = ""

    def start(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append(body)
                outer.auth.append(self.headers.get("Authorization", ""))
                payload = {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion",
                    "created": 0,
                    "model": body.get("model", ""),
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": '{"ok": true}'},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
                }
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_a) -> None:  # noqa: ANN002
                return

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self._server.server_port}/v1"

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()


@pytest.fixture()
def fake_openai():
    srv = _FakeOpenAI()
    srv.start()
    yield srv
    srv.stop()


def _client_for(srv: _FakeOpenAI, **over: Any) -> LLMClient:
    base: dict[str, Any] = {
        "llm_provider": "deepseek",
        "deepseek_api_key": "dk-secret",
        "llm_base_url": srv.base_url,
    }
    base.update(over)
    return LLMClient(Settings(**base))


def test_request_carries_provider_model_and_key(fake_openai) -> None:
    """**核心接线断言**：请求里带的是这家服务商的密钥与模型。

    "配置对象里字段是对的"证明不了这些字段真的上了线；这里直接看服务端收到了什么。
    """
    c = _client_for(fake_openai)
    out = c.chat_text("系统", "用户")

    assert out == '{"ok": true}'
    assert len(fake_openai.requests) == 1
    body = fake_openai.requests[0]
    assert body["model"] == "deepseek-chat"
    assert body["messages"][0]["role"] == "system"
    # 密钥必须真的放进 Authorization（且是本服务商的那把）。
    assert fake_openai.auth[0] == "Bearer dk-secret"


def test_vision_request_carries_image_parts(fake_openai, tmp_path) -> None:
    """视觉请求必须带 image_url 分片 —— 这是 VLM 审查的全部前提。

    用一个真实存在的 PNG 文件：`encode_image` 会把不存在的路径跳过，
    于是"没有图片分片"会被误判成"实现不支持图片"。
    """
    img = tmp_path / "frame.png"
    # 最小合法 PNG（1x1 透明像素）。
    img.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
            "1f15c4890000000a49444154789c6300010000050001od".replace("od", "0d")
            + "0a2db40000000049454e44ae426082"
        )
    )

    c = _client_for(fake_openai, vlm_provider="bailian", dashscope_api_key="bk-secret")
    from scidirector_ai.schemas import CriticFeedback  # noqa: F401  (仅确保导入可用)

    c.chat_json(
        "系统", "用户", _OkSchema, images=[str(img)], role="vision"
    )

    body = fake_openai.requests[0]
    assert body["model"] == PROVIDERS["bailian"].vlm_model, "视觉必须用百炼的视觉模型"
    assert fake_openai.auth[0] == "Bearer bk-secret", "视觉要用百炼的密钥，而不是文本那家"
    parts = body["messages"][-1]["content"]
    assert isinstance(parts, list) and any(p.get("type") == "image_url" for p in parts), (
        f"视觉请求里没有图片分片：{parts}"
    )


def test_vision_call_is_refused_when_provider_has_no_vision(fake_openai, tmp_path) -> None:
    """**安全规则**：真文本 + 无视觉能力 ⇒ 抛错（由 Critic 降级转人工），
    **绝不**返回 mock 的"审查通过"。

    这条规则防的是一类很危险的组合：文本接了真模型、视觉忘了配。
    若此时悄悄用 mock 审查，会返回伪造的通过结论，未审查的画面进成片，
    而且没有任何报错。
    """
    img = tmp_path / "frame.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    c = _client_for(fake_openai)  # 文本=deepseek（有密钥），视觉跟随=deepseek（无视觉）
    assert c.is_mock is False, "文本是真的，整体不该是 mock"

    with pytest.raises(LLMError) as ei:
        c.chat_json("系统", "用户", _OkSchema, images=[str(img)], role="vision")
    assert "视觉" in str(ei.value)
    # 关键：不能因为视觉不可用就退回 mock —— 那会返回伪造的审查通过。
    assert not fake_openai.requests, "不可用时不该发出任何请求"


def test_permanent_error_is_not_retried(fake_openai, monkeypatch) -> None:
    """401/404 这类错误重试没有意义：只应发一次请求。

    每次都白等退避会让失败反馈晚好几秒，并把日志刷满同样的信息。
    """

    class _Boom(Exception):
        status_code = 401

    calls = {"n": 0}

    def boom(**_kw):
        calls["n"] += 1
        raise _Boom("invalid api key")

    c = _client_for(fake_openai)
    monkeypatch.setattr(c._text_client.chat.completions, "create", boom)

    with pytest.raises(LLMError) as ei:
        c.chat_text("s", "u")
    assert calls["n"] == 1, f"配置类错误不该重试，实际调用了 {calls['n']} 次"
    # 错误信息必须**如实**说明没有重试过。写死"已重试 N 次"会让人去查
    # 网络抖动或限流，而真正的原因是密钥/模型名写错了。
    assert "未重试" in str(ei.value)
    assert "已尝试" not in str(ei.value)


def test_transient_error_is_retried(fake_openai, monkeypatch) -> None:
    """反向控制：真正的瞬时错误（5xx）**必须**重试，否则一次抖动就毁掉一个镜头。"""

    class _Flaky(Exception):
        status_code = 503

    calls = {"n": 0}
    orig = fake_openai  # noqa: F841 - 保持 fixture 存活

    c = _client_for(fake_openai)
    monkeypatch.setattr(c.settings.__class__, "llm_max_retries", 2, raising=False)

    real = c._text_client.chat.completions.create

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _Flaky("upstream hiccup")
        return real(**kw)

    monkeypatch.setattr(c._text_client.chat.completions, "create", flaky)
    monkeypatch.setattr("scidirector_ai.llm.time.sleep", lambda _s: None)  # 不真的等

    out = c.chat_text("s", "u")
    assert out == '{"ok": true}'
    assert calls["n"] == 2, "瞬时错误应当重试并成功"


from pydantic import BaseModel  # noqa: E402  (放在末尾以免打断阅读顺序)


class _OkSchema(BaseModel):
    ok: bool


# ---------------------------------------------------------------------------
# 3) 真实端点（用无效密钥验证"接线正确"）
# ---------------------------------------------------------------------------
#
# 没有真实密钥也能验证一件重要的事：**请求打到的是对的地方、并且被正确鉴权**。
# 本机实测：api.deepseek.com 与 dashscope.aliyuncs.com 可达，而 api.openai.com 不通。
# 因此这两家能验、OpenAI 只能跳过 —— 跳过原因要写清楚，不要假装验过了。
#
# 有效期内的密钥能拿到真实补全，那一步**不在**本用例范围内（需要密钥）。


def _endpoint_reachable(url: str) -> bool:
    import socket
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    try:
        with socket.create_connection((host, 443), timeout=5):
            return True
    except OSError:
        return False


@pytest.mark.parametrize("provider,key_attr", [("deepseek", "deepseek_api_key"), ("bailian", "dashscope_api_key")])
def test_real_endpoint_rejects_invalid_key_without_retrying(provider, key_attr) -> None:
    """真实端点 + 无效密钥：必须是**鉴权失败**，且**只发一次**请求。

    这条同时验证三件事，而且用的是真网络：
      1. base_url 指向的是该服务商的**兼容端点**（打错路径会 404 而不是 401）；
      2. 密钥确实被放进了 Authorization（不放会 401，但错误类型不同）；
      3. 401 被分类为"不可重试" —— 否则每次调用都白等 3 次退避。
    """
    spec = PROVIDERS[provider]
    if not _endpoint_reachable(spec.base_url):
        pytest.skip(
            f"{spec.base_url} 不可达（本机网络限制），跳过 {spec.label} 的真实端点验证"
        )

    s = Settings(llm_provider=provider, **{key_attr: "sk-invalid-probe-not-a-real-key"})
    c = LLMClient(s)

    calls = {"n": 0}
    real = c._text_client.chat.completions.create

    def counting(**kw):
        calls["n"] += 1
        return real(**kw)

    c._text_client.chat.completions.create = counting  # type: ignore[method-assign]

    with pytest.raises(LLMError) as ei:
        c.chat_text("你是助手", "只回复两个字：你好")

    msg = str(ei.value)
    assert "401" in msg or "authentication" in msg.lower() or "invalid" in msg.lower(), (
        f"期望鉴权失败，实际：{msg[:200]}"
    )
    assert calls["n"] == 1, f"鉴权错误不该重试，实际发出 {calls['n']} 次请求"
    assert "未重试" in msg, "错误信息要如实说明没有重试"


def test_openai_endpoint_is_unreachable_here_so_it_cannot_be_verified() -> None:
    """把"OpenAI 在本机验不了"这件事**显式记下来**，避免以后误以为验过。

    这条用例断言的是**环境现状**而不是产品行为 —— 一旦哪天网络通了它会失败，
    那时把它删掉、去补真实验证即可（失败信息里写了原因）。
    """
    if _endpoint_reachable(PROVIDERS["openai"].base_url):
        pytest.skip("api.openai.com 现在可达了：请把上面那条真实端点验证扩展到 openai")
    assert True
