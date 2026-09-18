"""OpenAI TTS 适配器。

## 关键限制：**不提供时间戳**

OpenAI 的 `audio.speech` 只返回音频，没有词/句级时间信息
（`audio.transcriptions` 有，但那是**识别**接口，不能用来合成）。
因此这条路径下字幕只能：
  1. 按镜头时长对齐（Go 侧 `PlanCuesWithNarration`），
  2. 镜头内部按文本比例分配。

这不是实现偷懒，而是接口能力的边界 —— 把它写清楚，免得有人以为「接了 OpenAI
字幕就一定准」。要更准就选带时间戳的服务商（Edge TTS / Azure / Fish Audio 的 timestamp 端点）。

## 鉴权

`OPENAI_API_KEY`，复用配置里已有的 `openai_api_key`（与 LLM/VLM 同一把密钥）。

## 失败分类

`AuthenticationError` / `BadRequestError`（音色或模型名不对）→ **不可重试**；
`RateLimitError` / `APIConnectionError` / 5xx → 可重试。
把「密钥无效」判成可重试会让编排层对着注定失败的调用烧满 attempt ——
这正是渲染引擎缺失时踩过的坑。
"""

from __future__ import annotations

from pathlib import Path

from .base import SynthesisResult, TTSError

DEFAULT_MODEL = "gpt-4o-mini-tts"
DEFAULT_VOICE = "alloy"

#: 单次请求的文本上限（保守取值）。超长请求会被服务端拒绝，
#: 而这种拒绝没有重试价值。
MAX_TEXT_CHARS = 4000


class OpenAITTSProvider:
    """基于 OpenAI `audio.speech` 的合成器。"""

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "",
        model: str = DEFAULT_MODEL,
        default_voice: str = DEFAULT_VOICE,
        timeout_sec: int = 60,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.default_voice = default_voice
        self.timeout_sec = timeout_sec

    def available(self) -> tuple[bool, str]:
        try:
            import openai  # noqa: F401
        except ImportError:
            return False, "未安装 openai（pip install openai）"
        if not self.api_key:
            return False, "未配置 SCID_OPENAI_API_KEY（OpenAI TTS 需要密钥）"
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
                f"文本 {len(clean)} 字超过上限 {MAX_TEXT_CHARS} 字，请先在导演侧拆分镜头",
                retryable=False,
                provider=self.name,
            )

        try:
            import openai
        except ImportError as err:  # pragma: no cover - available() 已拦
            raise TTSError(f"未安装 openai: {err}", retryable=False, provider=self.name) from err

        kwargs: dict[str, object] = {}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        client = openai.OpenAI(api_key=self.api_key, timeout=self.timeout_sec, **kwargs)

        speech_kwargs: dict[str, object] = {
            "model": self.model,
            "voice": voice or self.default_voice,
            "input": clean,
        }
        if speed is not None and speed > 0:
            speech_kwargs["speed"] = speed

        try:
            resp = client.audio.speech.create(**speech_kwargs)
            audio = resp.content
        except openai.AuthenticationError as err:
            raise TTSError(
                f"OpenAI TTS 鉴权失败（密钥无效或权限不足）: {err}",
                retryable=False,
                provider=self.name,
            ) from err
        except openai.BadRequestError as err:
            raise TTSError(
                f"OpenAI TTS 请求不被接受（模型/音色名可能不对）: {err}",
                retryable=False,
                provider=self.name,
            ) from err
        except (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError) as err:
            raise TTSError(f"OpenAI TTS 暂时不可用: {err}", retryable=True, provider=self.name) from err
        except Exception as err:  # noqa: BLE001 - 其余归为可重试，避免误判死局
            raise TTSError(f"OpenAI TTS 合成失败: {err}", retryable=True, provider=self.name) from err

        if not audio:
            raise TTSError("OpenAI TTS 返回了空音频", retryable=True, provider=self.name)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(audio)

        # duration 由调用方用 ffprobe 探测：这里不猜时长。
        # 猜错的后果是字幕与音频错位，而 ffprobe 就在手边。
        return SynthesisResult(
            audio_path=out_path, duration_sec=0.0, marks=(), provider=self.name
        )
