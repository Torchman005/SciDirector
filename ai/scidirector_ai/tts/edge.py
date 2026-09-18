"""Edge TTS 适配器（微软 Edge 的在线朗读服务，经 `edge-tts` 包调用）。

## 定位

这是**唯一在本机可以真实验证**的服务商：免费、不需要密钥、端点可达
（`speech.platform.bing.com` 实测 200）。因此它同时是**缺省推荐**与
「其他服务商还没配好时的兜底」。

## 它给什么时间戳（实测，不是文档抄来的）

返回的是 **句级** `SentenceBoundary`（不是词级 WordBoundary），带 offset 与 duration。
实测一段三句话的中文：

```
第一句讲勾股定理。   offset=0.100s duration=2.225s
第二句给出证明思路。 offset=2.275s duration=2.538s
第三句说明它的用途。 offset=4.812s duration=2.337s
```

这正好对上字幕侧的分句逻辑（`splitSentences`），**够用且免费** ——
比「按文本长度估算」精确得多，也是本机唯一能端到端验证的一条。

## 代价（必须写清楚）

* 用的是**非官方接口**：随时可能变更或失效，且属于 ToS 灰区；
* 因此**不保证 SLA**，产线若要稳定性应换 Azure 官方（同一引擎、带密钥）。

失败时按本项目约定分类：连不上/5xx/429 可重试；文本超长等属不可重试。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from .base import SentenceMark, SynthesisResult, TTSError

#: Edge TTS 的 offset/duration 单位是 100 纳秒。
_TICKS_PER_SEC = 10_000_000

#: 缺省中文音色。可在设置里覆盖。
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

#: 单次请求的文本上限。超长时 Edge 会直接断开而不是报错，
#: 表现为「合成到一半没了」，因此这里主动拦下并判为不可重试。
MAX_TEXT_CHARS = 2000


class EdgeTTSProvider:
    """基于 `edge-tts` 的合成器。"""

    name = "edge"

    def __init__(self, *, default_voice: str = DEFAULT_VOICE, timeout_sec: int = 60) -> None:
        self.default_voice = default_voice
        self.timeout_sec = timeout_sec

    def available(self) -> tuple[bool, str]:
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            return False, "未安装 edge-tts（pip install edge-tts）"
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
                f"文本 {len(clean)} 字超过 Edge TTS 单次上限 {MAX_TEXT_CHARS} 字"
                f"（超长时它会中途断开，而不是报错）",
                retryable=False,
                provider=self.name,
            )

        rate = None
        if speed is not None and speed > 0:
            # edge-tts 用相对百分比表示语速，1.0 表示不变。
            pct = round((speed - 1.0) * 100)
            rate = f"{pct:+d}%"

        try:
            audio, marks = asyncio.run(
                asyncio.wait_for(
                    self._stream(clean, voice or self.default_voice, rate),
                    timeout=self.timeout_sec,
                )
            )
        except asyncio.TimeoutError as err:
            raise TTSError(
                f"Edge TTS 超时（>{self.timeout_sec}s）", retryable=True, provider=self.name
            ) from err
        except TTSError:
            raise
        except Exception as err:  # noqa: BLE001 - 网络/协议异常统一归类
            raise TTSError(
                f"Edge TTS 合成失败: {err}", retryable=True, provider=self.name
            ) from err

        if not audio:
            # 拿到 0 字节却「成功」是最危险的结果：后续会把空音频当成成片音轨。
            raise TTSError("Edge TTS 返回了空音频", retryable=True, provider=self.name)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(audio)

        duration = max((m.end_sec for m in marks), default=0.0)
        return SynthesisResult(
            audio_path=out_path,
            duration_sec=duration,
            marks=tuple(marks),
            provider=self.name,
        )

    async def _stream(
        self, text: str, voice: str, rate: str | None
    ) -> tuple[bytes, list[SentenceMark]]:
        import edge_tts

        kwargs: dict[str, object] = {}
        if rate:
            kwargs["rate"] = rate
        communicate = edge_tts.Communicate(text, voice, **kwargs)

        audio = bytearray()
        marks: list[SentenceMark] = []
        async for chunk in communicate.stream():
            kind = chunk.get("type")
            if kind == "audio":
                audio.extend(chunk.get("data", b""))
            elif kind in ("SentenceBoundary", "WordBoundary"):
                # 两种都给：哪些服务端会发 WordBoundary 取决于音色与版本，
                # 拿到了就用，拿不到就退到句级。
                marks.append(
                    SentenceMark(
                        text=str(chunk.get("text", "")),
                        start_sec=float(chunk.get("offset", 0)) / _TICKS_PER_SEC,
                        duration_sec=float(chunk.get("duration", 0)) / _TICKS_PER_SEC,
                    )
                )
        return bytes(audio), marks
