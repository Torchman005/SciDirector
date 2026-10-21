"""带上下文的镜头配音：连读合成，再按逐字时间戳裁出本镜头。

## 为什么需要

旁白按镜头逐段合成时，每段只有一两句话。TTS 模型看不到前后文，
于是每段都"重新起调、句末降调"——整片听起来像七八条互不相干的播报，
这正是"机械、没有语调起伏"的主要来源（不是音色或采样率的问题）。

做法：把上一镜头的最后一句、本镜头全文、下一镜头的第一句连成一段合成，
模型据此给出连贯的语调走向；再用服务商返回的逐字时间戳，
在句间停顿的中点裁出本镜头的音频，时间戳同步平移。

只在服务商给逐字时间戳时启用（目前是 Fish）。任何一步对不上
（时间戳缺失、文本与对齐结果不一致、裁剪失败）都回退到单段合成——
宁可语调平一点，也不能把别的镜头的话裁进本镜头。
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

from .base import SentenceMark, SynthesisResult, TTSProvider, synthesize_with_retry

logger = logging.getLogger(__name__)

_SENTENCE = re.compile(r"[^。！？!?；;…]+[。！？!?；;…]*")
#: 不发音的字符：对齐结果里通常没有它们，比对时一并去掉。
_SILENT = re.compile(r"[\s　，。！？、；：,.!?;:…—\-–·“”‘’\"'（）()《》〈〉【】\[\]{}<>/\\|~～*#]+")
#: 裁剪点离相邻发音的最小余量；句间停顿过短时宁可贴近本镜头。
_PAD_SEC = 0.06
#: 本镜头首尾最多保留的停顿，避免把整段长停顿带进来。
_MAX_EDGE_SEC = 0.35


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.findall(text or "") if s.strip()]


def context_parts(prev: str, text: str, nxt: str, count: int) -> tuple[str, str]:
    """取上一镜头末尾、下一镜头开头各 ``count`` 句作为上下文。"""
    if count <= 0:
        return "", ""
    before = "".join(sentences(prev)[-count:])
    after = "".join(sentences(nxt)[:count])
    return before, after


def _ensure_break(text: str) -> str:
    text = text.strip()
    return text if not text or text[-1] in "。！？!?；;…，," else text + "。"


def _content(text: str) -> str:
    return _SILENT.sub("", text).casefold()


def _target_span(marks: tuple[SentenceMark, ...], before: str, text: str) -> tuple[int, int] | None:
    """返回本镜头覆盖的 marks 下标区间 [first, last]；对不上返回 None。"""
    start = len(_content(before))
    end = start + len(_content(text))
    if end <= start:
        return None
    offset, first, last = 0, None, None
    for index, mark in enumerate(marks):
        size = len(_content(mark.text))
        if size == 0:
            continue
        lo, hi = offset, offset + size
        # 一个对齐单元跨越镜头边界说明分词与文本对不上，放弃裁剪。
        if lo < start < hi or lo < end < hi:
            return None
        if lo >= start and hi <= end:
            first = index if first is None else first
            last = index
        offset = hi
    return (first, last) if first is not None and last is not None else None


def _cut(src: Path, dst: Path, start: float, end: float) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("找不到 ffmpeg，无法裁剪上下文配音")
    duration = end - start
    fade = min(0.012, duration / 4)
    cmd = [ffmpeg, "-v", "error", "-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(src),
           "-af", f"afade=t=in:d={fade:.3f},afade=t=out:st={max(duration - fade, 0):.3f}:d={fade:.3f}",
           "-c:a", "libmp3lame", "-b:a", "192k", str(dst)]
    proc = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    if proc.returncode != 0 or not dst.is_file() or dst.stat().st_size == 0:
        raise RuntimeError("ffmpeg 裁剪失败：" + proc.stderr.decode("utf-8", "replace")[-300:])


def synthesize_in_context(
    provider: TTSProvider,
    text: str,
    *,
    out_path: Path,
    prev_text: str = "",
    next_text: str = "",
    context_sentences: int = 1,
    attempts: int = 3,
    backoff_sec: float = 1.0,
) -> SynthesisResult:
    """连读合成本镜头；不具备条件时等价于 ``synthesize_with_retry``。"""
    before, after = context_parts(prev_text, text, next_text, context_sentences)
    plain = lambda: synthesize_with_retry(  # noqa: E731 - 单一回退路径
        provider, text, out_path=out_path, attempts=attempts, backoff_sec=backoff_sec)
    if not (before or after) or getattr(provider, "name", "") != "fish":
        return plain()

    out_path = Path(out_path).resolve()
    joined = _ensure_break(before) + _ensure_break(text) + after.strip()
    full_path = out_path.with_name(out_path.stem + ".context" + out_path.suffix)
    try:
        full = synthesize_with_retry(provider, joined, out_path=full_path,
                                     attempts=attempts, backoff_sec=backoff_sec)
        span = _target_span(full.marks, _ensure_break(before), text)
        if span is None:
            raise ValueError("逐字时间戳与文本对不上")
        first, last = span
        marks = full.marks
        speech_start, speech_end = marks[first].start_sec, marks[last].end_sec
        prev_end = marks[first - 1].end_sec if first > 0 else 0.0
        next_start = marks[last + 1].start_sec if last + 1 < len(marks) else None
        # 在句间停顿的中点裁剪，但首尾停顿各不超过 _MAX_EDGE_SEC。
        cut_start = max(speech_start - _MAX_EDGE_SEC, (prev_end + speech_start) / 2, 0.0)
        cut_start = min(cut_start, max(speech_start - _PAD_SEC, 0.0))
        if next_start is None:
            cut_end = speech_end + _MAX_EDGE_SEC
        else:
            cut_end = min(speech_end + _MAX_EDGE_SEC, (speech_end + next_start) / 2)
            cut_end = max(cut_end, min(speech_end + _PAD_SEC, next_start))
        _cut(Path(full.audio_path), out_path, cut_start, cut_end)
    except Exception as exc:  # noqa: BLE001 - 任一环节失败都回退到单段合成
        logger.warning("上下文连读合成失败，回退到单段合成", extra={"error": str(exc)[:200]})
        return plain()
    finally:
        full_path.unlink(missing_ok=True)

    shifted = tuple(SentenceMark(text=m.text, start_sec=round(m.start_sec - cut_start, 4),
                                 duration_sec=m.duration_sec)
                    for m in marks[first:last + 1])
    return SynthesisResult(audio_path=out_path, duration_sec=max(m.end_sec for m in shifted),
                           marks=shifted, provider=full.provider)
