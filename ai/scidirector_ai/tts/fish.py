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

import base64
import re
from pathlib import Path

import httpx

from .base import SentenceMark, SynthesisResult, TTSError, write_audio

DEFAULT_ENDPOINT = "https://api.fish.audio/v1/tts"
#: 带**逐字时间戳**的端点（非流式，返回 JSON）。
#
# 真机探测出来的结构（此前"规范里没定义"所以没实现，现在直接问 API 拿到了）：
#   POST /v1/tts/with-timestamp -> 200 application/json
#   {"audio_base64": "...", "text": "...",
#    "alignment": [{"text":"第","start":0.0,"end":0.32}, ...]}
#
# **逐字**的 start/end —— 比句级还细。这正是"语音和字幕对不上"的解法：
# 原来没有时间戳，字幕只能按"镜头时长 × 文本比例"猜，长镜头里能差好几秒。
TIMESTAMP_ENDPOINT = "https://api.fish.audio/v1/tts/with-timestamp"
DEFAULT_MODEL = "s1"

#: Fish Audio 的音色 id 形如 32 位十六进制（见官方示例）。
_REFERENCE_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")

#: 句子结束标点：拿它把逐字对齐切成句级 mark。
_SENTENCE_END = "。！？；…!?;"


def marks_from_alignment(alignment: object) -> tuple[SentenceMark, ...]:
    """把 Fish 的**逐字对齐**合并成**句级** marks。

    两种形状都要支持，因为流式与非流式端点给的不一样：
      * 非流式：`[{"text","start","end"}, ...]`（扁平列表）
      * 流式：  `{"segments": [...], "audio_duration": N}`

    合并规则：遇到句末标点就收一句；末尾残余也收一句（否则最后半句会丢字幕）。
    时间取**首字 start 到末字 end**，而不是按字数比例 —— 后者正是"字幕比语音
    早/晚一点"的来源：模型念每个字的时长本来就不一样。
    """
    segments: list = []
    if isinstance(alignment, dict):
        raw = alignment.get("segments")
        if isinstance(raw, list):
            segments = raw
    elif isinstance(alignment, list):
        segments = alignment

    marks: list[SentenceMark] = []
    buf: list[str] = []
    start: float | None = None
    last_end = 0.0

    for seg in segments:
        if not isinstance(seg, dict):
            continue
        ch = str(seg.get("text") or "")
        if not ch:
            continue
        try:
            s = float(seg.get("start"))
            e = float(seg.get("end"))
        except (TypeError, ValueError):
            continue
        if start is None:
            start = s
        buf.append(ch)
        last_end = max(last_end, e, s)
        if ch[-1] in _SENTENCE_END:
            marks.append(SentenceMark(
                text="".join(buf), start_sec=start or 0.0,
                duration_sec=max(0.0, last_end - (start or 0.0)),
            ))
            buf, start = [], None

    if buf and start is not None:
        marks.append(SentenceMark(
            text="".join(buf), start_sec=start,
            duration_sec=max(0.0, last_end - start),
        ))
    return tuple(marks)

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
        #: 是否使用带**逐字时间戳**的端点。
        #
        # **默认 False（尚未启用）**，原因如实说明：真机探测已经拿到结构
        # （`/v1/tts/with-timestamp` -> `{audio_base64, text, alignment:[{text,start,end}]}`），
        # marks 也确实能产出了；但还剩一步没做完 ——
        # **API 的 alignment 不含标点**，所以"遇到句末标点就切句"永远切不开，
        # 目前只会产出 1 条覆盖全文的 mark。正确做法是拿 alignment 去对齐
        # **原始文本**、按原文里的标点切句。
        #
        # 在那一步做完之前保持关闭：开着会退化成"整段一句话"，比按文本比例
        # 猜还要粗，反而更差。打开它就是一行配置的事。
        with_timestamp: bool = False,
    ) -> None:
        self.api_key = api_key
        self.endpoint = endpoint
        self.model = model
        self.reference_id = reference_id
        self.audio_format = audio_format
        self.timeout_sec = timeout_sec
        self.with_timestamp = with_timestamp

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
            # **先验形状，再发请求。**
            #
            # 这是一道防呆，来历很具体：切换 TTS 服务商时，`SCID_TTS_VOICE` 里
            # 常常还留着上一家的音色名（例如豆包的 `custom_zh_clone_agent`），
            # 而它在这里**优先于** `SCID_FISH_REFERENCE_ID`。于是请求带着一个
            # 别家的音色名发出去，服务端只会回一个与"音色配错了"毫无关系的错，
            # 排查方向直接跑偏。
            #
            # Fish 的音色 id 是 32 位十六进制，形状足够特征化，值得当场拦下。
            if not _REFERENCE_ID_RE.match(ref.strip()):
                raise TTSError(
                    f"音色 id {ref!r} 不是 Fish Audio 的 reference_id"
                    "（应为 32 位十六进制，例如 0dcdcfacd3934bb799c38498b507e5c5）。"
                    " 最常见的原因是 SCID_TTS_VOICE 里还留着**别的服务商**的音色名 ——"
                    "它优先于 SCID_FISH_REFERENCE_ID。请把它换成 Fish 的音色 id，或清空。",
                    retryable=False,
                    provider=self.name,
                )
            payload["reference_id"] = ref

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.model:
            headers["model"] = self.model

        # 选端点：要时间戳就走 with-timestamp —— **字幕对齐完全靠它**。
        # 只在自己没显式配端点时才切换：用户显式配的地址优先。
        endpoint = self.endpoint
        want_ts = self.with_timestamp and endpoint == DEFAULT_ENDPOINT
        if want_ts:
            endpoint = TIMESTAMP_ENDPOINT

        try:
            resp = httpx.post(
                endpoint, json=payload, headers=headers, timeout=self.timeout_sec
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

        # 带时间戳的端点返回 **JSON**（音频在 base64 里，另带逐字 alignment），
        # 而不带时间戳的端点直接返回二进制音频。两者必须分开处理 ——
        # 把 JSON 当音频落盘会得到"看似有内容、播放器打不开"的文件。
        marks: tuple[SentenceMark, ...] = ()
        if want_ts:
            try:
                body = resp.json()
            except ValueError as err:
                raise TTSError(
                    f"Fish Audio 时间戳端点返回的不是 JSON（接口形状可能变了）: "
                    f"{resp.content[:200]!r}",
                    retryable=False, provider=self.name,
                ) from err
            b64 = body.get("audio_base64") or ""
            if not b64:
                raise TTSError(
                    f"Fish Audio 时间戳端点没有 audio_base64（键：{list(body)[:6]}）",
                    retryable=False, provider=self.name,
                )
            try:
                audio = base64.b64decode(b64)
            except (ValueError, TypeError) as err:
                raise TTSError(
                    f"Fish Audio 音频 base64 解码失败: {err}",
                    retryable=False, provider=self.name,
                ) from err
            marks = marks_from_alignment(body.get("alignment"))
        else:
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
            # marks 一定要传出去：它由时间戳端点解析而来，Go 侧据此把字幕
            # 对齐到**真实说话时刻**而不是按文本比例猜。
            # （这里曾经硬编码 ms=() ，于是"接了时间戳"看起来毫无效果。）
            audio_path=written, duration_sec=0.0, marks=marks, provider=self.name
        )
