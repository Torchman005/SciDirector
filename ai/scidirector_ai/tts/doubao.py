"""豆包语音（火山引擎）TTS 适配器。

## ⚠️ 验证状态：**未经真实验证**（本机没有凭据，且官方文档页是 JS 壳、抓不到正文）

这条适配器是按火山引擎「语音合成大模型 · HTTP 非流式接口 V1」的公开接口形状写的，
但**我没有在本机跑通过它**：没有 appid/token，也无法从文档页读到权威参数表
（`docs.volcengine.com` 返回的是前端壳，正文由 JS 渲染）。

因此这里做了三件事把风险压到最低：

1. **端点、cluster、成功码、字段名全部可配置** —— 真实形状与预期不符时，
   不必改代码就能纠正；
2. **响应严格校验**：拿不到成功码就报错，并把服务端返回的 `code`/`message` 原样带出来，
   而不是「解析失败」这种没法排查的信息；
3. **明确标注**：代码注释、配置项说明与 `docs/ROADMAP.md` 都写明它尚未验证。

**首次接入时请按官方文档核对 `build_payload` 的字段名**，那是唯一可能与现实不符的地方。

## 时间戳

V1 接口是否支持返回时间信息取决于参数与版本，我**没有可核实的依据**，
因此这里不臆造字段：`marks` 恒为空，字幕回退到「按镜头真实音频时长对齐」
（Go 侧已实现并验证）。需要更细的对齐请用 Edge TTS 或 Fish Audio 的 timestamp 端点。
"""

from __future__ import annotations

import base64
import uuid
from pathlib import Path

import httpx

from .base import SynthesisResult, TTSError

DEFAULT_ENDPOINT = "https://openspeech.bytedance.com/api/v1/tts"
DEFAULT_CLUSTER = "volcano_tts"
DEFAULT_VOICE = "zh_female_shuangkuaisisi_moon_bigtts"
#: 官方约定的成功码。
SUCCESS_CODE = 3000

MAX_TEXT_CHARS = 1000


class DoubaoTTSProvider:
    """火山引擎（豆包）语音合成。"""

    name = "doubao"

    def __init__(
        self,
        *,
        appid: str = "",
        access_token: str = "",
        cluster: str = DEFAULT_CLUSTER,
        endpoint: str = DEFAULT_ENDPOINT,
        default_voice: str = DEFAULT_VOICE,
        encoding: str = "mp3",
        sample_rate: int = 24000,
        timeout_sec: int = 60,
    ) -> None:
        self.appid = appid
        self.access_token = access_token
        self.cluster = cluster
        self.endpoint = endpoint
        self.default_voice = default_voice
        self.encoding = encoding
        self.sample_rate = sample_rate
        self.timeout_sec = timeout_sec

    def available(self) -> tuple[bool, str]:
        if not self.appid:
            return False, "未配置 SCID_DOUBAO_APPID（火山引擎语音合成需要 appid）"
        if not self.access_token:
            return False, "未配置 SCID_DOUBAO_ACCESS_TOKEN"
        return True, ""

    def build_payload(self, text: str, voice: str, speed: float | None) -> dict:
        """构造请求体。

        **首次接入时请对着官方文档核对这里的字段名** —— 这是本适配器唯一
        可能与现实不符的地方（其余部分：鉴权头、成功码、base64 音频都是稳定的）。
        """
        audio: dict[str, object] = {
            "voice_type": voice,
            "encoding": self.encoding,
            "sample_rate": self.sample_rate,
        }
        if speed is not None and speed > 0:
            audio["speed_ratio"] = round(speed, 2)

        return {
            "app": {"appid": self.appid, "token": self.access_token, "cluster": self.cluster},
            "user": {"uid": "scidirector"},
            "audio": audio,
            "request": {
                "reqid": str(uuid.uuid4()),
                "text": text,
                "operation": "query",
            },
        }

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

        payload = self.build_payload(clean, voice or self.default_voice, speed)
        # 火山引擎的鉴权头格式是 `Bearer;<token>`（分号，不是空格）。
        # 写成空格会得到鉴权失败，而错误信息里通常不会点明这一点。
        headers = {
            "Authorization": f"Bearer;{self.access_token}",
            "Content-Type": "application/json",
        }

        try:
            resp = httpx.post(
                self.endpoint, json=payload, headers=headers, timeout=self.timeout_sec
            )
        except httpx.HTTPError as err:
            raise TTSError(f"豆包 TTS 网络错误: {err}", retryable=True, provider=self.name) from err

        if resp.status_code >= 500:
            raise TTSError(
                f"豆包 TTS 服务端错误 HTTP {resp.status_code}", retryable=True, provider=self.name
            )
        if resp.status_code >= 400:
            # 4xx 里最可能是鉴权/参数问题 —— 重试没有意义。
            raise TTSError(
                f"豆包 TTS 请求被拒 HTTP {resp.status_code}: {resp.text[:200]}",
                retryable=False,
                provider=self.name,
            )

        try:
            body = resp.json()
        except ValueError as err:
            raise TTSError(
                f"豆包 TTS 返回的不是 JSON（接口形状可能与预期不符）: {resp.text[:200]}",
                retryable=False,
                provider=self.name,
            ) from err

        code = body.get("code")
        if code != SUCCESS_CODE:
            raise TTSError(
                f"豆包 TTS 返回失败码 code={code} message={body.get('message')!r}"
                f"（成功码应为 {SUCCESS_CODE}）",
                # 失败码多为参数/权限问题，重试同样会失败；限流属少数派，
                # 交由上层按整体策略决定是否重投任务。
                retryable=False,
                provider=self.name,
            )

        data = body.get("data") or ""
        if not data:
            raise TTSError("豆包 TTS 返回成功码但没有音频数据", retryable=True, provider=self.name)

        try:
            audio = base64.b64decode(data)
        except (ValueError, TypeError) as err:
            raise TTSError(f"豆包 TTS 音频 base64 解码失败: {err}", retryable=False, provider=self.name) from err
        if not audio:
            raise TTSError("豆包 TTS 解码出空音频", retryable=True, provider=self.name)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(audio)
        return SynthesisResult(
            audio_path=out_path, duration_sec=0.0, marks=(), provider=self.name
        )
