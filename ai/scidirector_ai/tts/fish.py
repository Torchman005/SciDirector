"""Fish Audio TTS 适配器。

## 接口形状的来源

来自 Fish Audio 的 OpenAPI 规范（`fish-audio-tts-api-openapi.yml`，实测可取）：

* `POST https://api.fish.audio/v1/tts`
* 鉴权：`Authorization: Bearer <api-key>`（密钥在 fish.audio 控制台签发）
* 可选请求头 `model: s1 | s2-pro`
* 请求体：`{text, reference_id?, format?: mp3|wav|pcm|opus, latency?, prosody?}`
* 响应：二进制音频（`audio/mpeg`）

## ⚠️ 验证状态：**未经真实验证**

本机到 `api.fish.audio` 的连接不通（`curl` 无响应），因此这条适配器
只按规范写就、未跑通过。与豆包适配器同样处理：端点/模型/音色可配置、
响应严格校验、并在此明确标注。

## 时间戳：接口有，但**故意没实现**

Fish Audio 另有 `POST /v1/tts/stream/with-timestamp`（SSE，返回音频分片 + 时间信息）。
但可获取到的规范里**只写了 `text/event-stream`、没有定义事件负载结构**，
我无法在不臆造字段的前提下实现它。

因此这里只用不返回时间戳的 `/v1/tts`，字幕回退到「按镜头真实音频时长对齐」。
要更细的对齐，当前**本机可真实验证**的选择是 Edge TTS（句级时间戳）。
待拿到该 SSE 的事件结构后，再补上即可 —— 适配器接口已经容得下（`marks` 字段现成）。
"""

from __future__ import annotations

from pathlib import Path

import httpx

from .base import SynthesisResult, TTSError, write_audio

DEFAULT_ENDPOINT = "https://api.fish.audio/v1/tts"
DEFAULT_MODEL = "s1"

MAX_TEXT_CHARS = 4000


class FishAudioTTSProvider:
    """Fish Audio 语音合成。"""

    name = "fish"

    def __init__(
        self,
        *,
        api_key: str = "",
        endpoint: str = DEFAULT_ENDPOINT,
        model: str = DEFAULT_MODEL,
        reference_id: str = "",
        audio_format: str = "mp3",
        timeout_sec: int = 60,
    ) -> None:
        self.api_key = api_key
        self.endpoint = endpoint
        self.model = model
        self.reference_id = reference_id
        self.audio_format = audio_format
        self.timeout_sec = timeout_sec

    def available(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "未配置 SCID_FISH_API_KEY（Fish Audio 需要密钥）"
        return True, ""

    def synthesize(
        self,
        text: str,
        *,
        out_path: Path,
        voice: str | None = None,
        speed: float | None = None,
    ) -> SynthesisResult:
        ok, reason = self.available()
        if not ok:
            raise TTSError(reason, retryable=False, provider=self.name)

        clean = text.strip()
        if not clean:
            raise TTSError("文本为空，无需合成", retryable=False, provider=self.name)
        if len(clean) > MAX_TEXT_CHARS:
            raise TTSError(
                f"文本 {len(clean)} 字超过单次上限 {MAX_TEXT_CHARS} 字，请先在导演侧拆分镜头",
                retryable=False,
                provider=self.name,
            )

        payload: dict[str, object] = {"text": clean, "format": self.audio_format}
        # voice 参数在这里表示 Fish Audio 的音色模型 id（reference_id）。
        ref = voice or self.reference_id
        if ref:
            payload["reference_id"] = ref

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.model:
            headers["model"] = self.model

        try:
            resp = httpx.post(
                self.endpoint, json=payload, headers=headers, timeout=self.timeout_sec
            )
        except httpx.HTTPError as err:
            raise TTSError(f"Fish Audio 网络错误: {err}", retryable=True, provider=self.name) from err

        if resp.status_code in (401, 403):
            raise TTSError(
                f"Fish Audio 鉴权失败 HTTP {resp.status_code}（密钥无效或权限不足）",
                retryable=False,
                provider=self.name,
            )
        if resp.status_code == 429:
            raise TTSError("Fish Audio 触发限流", retryable=True, provider=self.name)
        if resp.status_code >= 500:
            raise TTSError(
                f"Fish Audio 服务端错误 HTTP {resp.status_code}", retryable=True, provider=self.name
            )
        if resp.status_code >= 400:
            raise TTSError(
                f"Fish Audio 请求被拒 HTTP {resp.status_code}: {resp.text[:200]}",
                retryable=False,
                provider=self.name,
            )

        audio = resp.content
        if not audio:
            raise TTSError("Fish Audio 返回了空音频", retryable=True, provider=self.name)

        # 防御性检查：正常应返回二进制音频。若拿到 JSON（例如把错误包成 200），
        # 直接落盘会产出一个「看起来有内容、播放器打不开」的文件 —— 那比报错更难查。
        if audio[:1] == b"{":
            raise TTSError(
                f"Fish Audio 返回的像是 JSON 而不是音频（接口形状可能与预期不符）: {audio[:200]!r}",
                retryable=False,
                provider=self.name,
            )

        # 统一走 write_audio：它会 resolve 并返回绝对路径，
        # 跨进程交给 Go worker 时相对路径必然失效。
        written = write_audio(out_path, audio)
        return SynthesisResult(
            audio_path=written, duration_sec=0.0, marks=(), provider=self.name
        )
