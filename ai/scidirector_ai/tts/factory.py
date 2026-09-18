"""按配置构造 TTS 服务商。

## 缺省是「不合成」

`SCID_TTS_PROVIDER` 为空/`none` 时返回 `None`，表示这条链路整体不启用 ——
与归档后端默认 `none`、渲染引擎缺失时熔断是同一套取舍：
**缺省必须是一条能跑通的路径**，不能因为没配 TTS 就让整个流水线失败。

## 服务商可用性如何呈现

`available()` 必须给出「为什么不可用 + 该做什么」。拿到 `(False, 原因)` 的调用方
应当**跳过配音并留痕**，而不是把它当成任务失败：没配音的成片是可用产物，
因缺密钥而失败的任务不是。
"""

from __future__ import annotations

from .base import TTSProvider

#: 不启用 TTS 的取值。与配置里的字面量保持一致。
DISABLED = frozenset({"", "none", "off", "disabled"})

#: 支持的服务商。写成显式集合而不是「try import」，
#: 是为了让配置写错时立刻报错，而不是悄悄退化成「没有 TTS」。
SUPPORTED = ("edge", "doubao", "openai", "fish")


def build_tts_provider(settings, *, provider: str | None = None) -> TTSProvider | None:
    """构造服务商实例；未启用时返回 None。

    按需导入各适配器：用 Edge TTS 的人不该被迫装上豆包/OpenAI 的 SDK，
    更不该因为某个 SDK 缺失而让整个进程起不来。
    """
    name = (provider if provider is not None else getattr(settings, "tts_provider", "")) or ""
    name = name.strip().lower()

    if name in DISABLED:
        return None

    if name not in SUPPORTED:
        raise ValueError(
            f"未知的 TTS 服务商 {name!r}；可选：{', '.join(SUPPORTED)}，或留空表示不合成"
        )

    voice = (getattr(settings, "tts_voice", "") or "").strip() or None
    speed = getattr(settings, "tts_speed", 1.0) or None

    if name == "edge":
        from .edge import EdgeTTSProvider

        return EdgeTTSProvider(
            default_voice=voice or "zh-CN-XiaoxiaoNeural",
            timeout_sec=getattr(settings, "tts_timeout_sec", 60),
        )

    if name == "doubao":
        from .doubao import DoubaoTTSProvider

        return DoubaoTTSProvider(
            appid=getattr(settings, "doubao_appid", ""),
            access_token=getattr(settings, "doubao_access_token", ""),
            cluster=getattr(settings, "doubao_cluster", "volcano_tts"),
            endpoint=getattr(settings, "doubao_endpoint", "") or "https://openspeech.bytedance.com/api/v1/tts",
            default_voice=voice or "zh_female_shuangkuaisisi_moon_bigtts",
            timeout_sec=getattr(settings, "tts_timeout_sec", 60),
        )

    if name == "openai":
        from .openai_tts import OpenAITTSProvider

        return OpenAITTSProvider(
            api_key=getattr(settings, "openai_api_key", ""),
            base_url=getattr(settings, "openai_base_url", ""),
            model=getattr(settings, "openai_tts_model", "gpt-4o-mini-tts"),
            default_voice=voice or "alloy",
            timeout_sec=getattr(settings, "tts_timeout_sec", 60),
        )

    # fish
    from .fish import FishAudioTTSProvider

    return FishAudioTTSProvider(
        api_key=getattr(settings, "fish_api_key", ""),
        endpoint=getattr(settings, "fish_endpoint", "") or "https://api.fish.audio/v1/tts",
        model=getattr(settings, "fish_model", "s1"),
        reference_id=getattr(settings, "fish_reference_id", ""),
        timeout_sec=getattr(settings, "tts_timeout_sec", 60),
    )
