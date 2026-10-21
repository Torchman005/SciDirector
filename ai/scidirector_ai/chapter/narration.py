"""章节旁白：整章一次合成，得到每句、每个字的真实时刻。

## 为什么先定旁白再做画面

逐镜头合成时，画面只能按"镜头时长 × 比例"去猜节拍。参考成片的做法是反过来：
先把整章旁白一次读完，拿到逐字时间戳，画面再写成"说到'牛奶'后 0.55 秒高亮"。
这样声画同步精确到字，而且整章语调连贯（不会每句重新起调）。

## 时间轴结构（与 chapter_runtime.js 的约定）

    {"dur": 总时长, "lead": 片头静音, "lines": [
        {"id", "sub", "start", "end", "chars": "去掉标点后的朗读文本", "times": [每个字的开始秒]}]}

`chars/times` 供运行时的 ``K(id, "牛奶")`` 查关键词时刻。所有时间已含片头静音。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from ..tts.base import SentenceMark, TTSProvider, synthesize_with_retry
from ..tts.context import _SILENT

#: 片头留白：画面先建立，再开口。
LEAD_SEC = 1.0
#: 片尾停留：最后一句说完后保留结论画面。
TAIL_SEC = 1.8
#: 单次合成上限与 Fish 一致。
MAX_CHAPTER_CHARS = 4000
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")


@dataclass(frozen=True)
class ChapterLine:
    id: str
    sub: str
    #: 朗读文本；为空时朗读字幕本身。用于去掉字幕里的空格、把 √d 读成"根号 d"等。
    say: str = ""

    @property
    def spoken(self) -> str:
        return (self.say or self.sub).strip()


def _content(text: str) -> str:
    return _SILENT.sub("", text).casefold()


def validate_lines(lines: list[ChapterLine]) -> None:
    ids = [line.id for line in lines]
    if not lines:
        raise ValueError("章节至少需要一句旁白")
    if len(set(ids)) != len(ids) or not all(_ID.match(i) for i in ids):
        raise ValueError("旁白 id 必须唯一，且为字母开头的字母/数字/下划线")
    if any(not _content(line.spoken) for line in lines):
        raise ValueError("每句旁白都必须有可朗读的文字")
    if sum(len(line.spoken) for line in lines) > MAX_CHAPTER_CHARS:
        raise ValueError(f"章节旁白超过 {MAX_CHAPTER_CHARS} 字，请拆成多个章节")


def _joined(lines: list[ChapterLine]) -> str:
    parts = []
    for line in lines:
        text = line.spoken
        parts.append(text if text[-1] in "。！？!?…" else text + "。")
    return "".join(parts)


def align_lines(lines: list[ChapterLine], marks: tuple[SentenceMark, ...], *,
                lead: float = LEAD_SEC, audio_sec: float = 0.0) -> dict:
    """把逐字时间戳对到每句旁白上。

    服务商会做文本规范化（"2017" 读成"二零一七"），所以不能按字数直接切；
    用序列比对把原稿的每个字映射到发音单元，对不上的字按相邻字插值。
    """
    script = [(_content(line.spoken), line) for line in lines]
    script_chars = "".join(chars for chars, _ in script)
    spoken = [(_content(m.text), m) for m in marks]
    spoken_chars, owner = "", []
    for chars, mark in spoken:
        for k, ch in enumerate(chars):
            spoken_chars += ch
            # 一个发音单元含多个字时，在其时长内均分。
            owner.append(mark.start_sec + mark.duration_sec * k / max(len(chars), 1))
    if not spoken_chars:
        raise ValueError("服务商没有返回逐字时间戳，无法生成章节时间轴")
    times: list[float | None] = [None] * len(script_chars)
    for block in SequenceMatcher(None, script_chars, spoken_chars, autojunk=False).get_matching_blocks():
        for k in range(block.size):
            times[block.a + k] = owner[block.b + k]
    matched = sum(t is not None for t in times)
    if matched < len(script_chars) * .6:
        raise ValueError(f"时间戳与旁白文本只对上 {matched}/{len(script_chars)} 字，拒绝生成错位的时间轴")
    end_of_speech = max(m.end_sec for m in marks)
    # 未对上的字：在前后已知时刻之间线性插值。
    known = [i for i, t in enumerate(times) if t is not None]
    for i, t in enumerate(times):
        if t is not None:
            continue
        before = max((k for k in known if k < i), default=None)
        after = min((k for k in known if k > i), default=None)
        if before is None:
            times[i] = times[after]  # type: ignore[index]
        elif after is None:
            times[i] = min(end_of_speech, times[before] + .2)  # type: ignore[operator]
        else:
            f = (i - before) / (after - before)
            times[i] = times[before] + (times[after] - times[before]) * f  # type: ignore[operator]
    out, offset = [], 0
    for index, (chars, line) in enumerate(script):
        own = times[offset:offset + len(chars)]
        offset += len(chars)
        next_start = times[offset] if offset < len(times) else end_of_speech
        # 句末：最后一个字的结束 ≈ 下一个字的开始；不超过下一句开头。
        last = own[-1] + .25  # type: ignore[operator]
        end = min(last, next_start) if index + 1 < len(script) else min(last, end_of_speech + .05)
        out.append({"id": line.id, "sub": line.sub, "start": round(own[0] + lead, 3),  # type: ignore[operator]
                    "end": round(max(end, own[0] + .2) + lead, 3), "chars": chars,  # type: ignore[operator]
                    "times": [round(t + lead, 3) for t in own]})  # type: ignore[operator]
    speech = max(audio_sec, end_of_speech)
    return {"dur": round(speech + lead + TAIL_SEC, 3), "lead": lead, "lines": out}


def estimate_timing(lines: list[ChapterLine], *, chars_per_sec: float = 4.6,
                    pause_sec: float = .32, lead: float = LEAD_SEC) -> dict:
    """无 TTS 时的估算时间轴：只用于离线预览与测试，不代表真实发音时刻。"""
    validate_lines(lines)
    t, marks = 0.0, []
    for line in lines:
        for ch in _content(line.spoken):
            marks.append(SentenceMark(ch, t, 1 / chars_per_sec))
            t += 1 / chars_per_sec
        t += pause_sec
    return align_lines(lines, tuple(marks), lead=lead)


def synthesize_chapter(provider: TTSProvider, lines: list[ChapterLine], out_dir: str | Path, *,
                       attempts: int = 3, backoff_sec: float = 1.0) -> tuple[Path, dict]:
    """整章一次合成；返回 (旁白音频, 时间轴)，时间轴同时写到 timing.json。"""
    validate_lines(lines)
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    result = synthesize_with_retry(provider, _joined(lines), out_path=out / "narration.mp3",
                                   attempts=attempts, backoff_sec=backoff_sec)
    if not result.marks:
        raise ValueError(f"{result.provider or '该服务商'} 没有返回逐字时间戳；章节模式需要 Fish 等带时间戳的服务")
    timing = align_lines(lines, result.marks, audio_sec=result.duration_sec)
    (out / "timing.json").write_text(json.dumps(timing, ensure_ascii=False, indent=1), encoding="utf-8")
    return Path(result.audio_path), timing
