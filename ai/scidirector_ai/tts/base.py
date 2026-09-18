"""TTS 服务商抽象层。

## 为什么要有这一层

四家服务商（Edge TTS / 豆包 / OpenAI / Fish Audio）的差异**不在音质**，而在两件事：

1. **能不能拿到时间戳**。这直接决定字幕能做到多准：
   - 有句级时间戳（Edge TTS 实测返回 `SentenceBoundary`，带 offset/duration）→
     字幕可以按**真实句子起止**对齐；
   - 只有音频（如 OpenAI TTS）→ 只能按镜头时长对齐，镜头内部仍按文本比例分配。
2. **鉴权方式**（无密钥 / Bearer / appid+token+cluster）。

把这两件事收在适配器里，上层（图节点、字幕对齐）就只需要面对一个统一结果。

## 时间戳的落点

时间戳通过**音频文件旁边的 sidecar JSON** 交给 Go 侧，而不是改进 proto：

    <audio_path>.marks.json

理由与 `payload_json` 当初的选择一致 —— 这个结构还在演进，走 JSON 可以不必每次
重新生成两侧代码；而且它是**可选增强**：没有 sidecar 时 Go 侧回退到「按镜头真实
音频时长对齐」（已实现并验证），链路不会因为服务商不给时间戳而断掉。

## 错误语义

沿用本项目的约定：错误必须**分类**（可重试 / 不可重试）。
把「密钥没配」当成可重试，会让编排层对着一个注定失败的调用烧满 attempt ——
这正是渲染引擎缺失时踩过的坑。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class SentenceMark:
    """一句（或一个分句）在音频里的位置。"""

    text: str
    start_sec: float
    duration_sec: float

    @property
    def end_sec(self) -> float:
        return self.start_sec + self.duration_sec


@dataclass(frozen=True)
class SynthesisResult:
    """一次合成的产物。"""

    audio_path: Path
    duration_sec: float
    #: 句级时间戳。为空表示该服务商不提供 —— 调用方必须能处理这种情形。
    marks: tuple[SentenceMark, ...] = ()
    #: 服务商名字，写进日志与 sidecar，便于事后判断「这条音频是谁产的」。
    provider: str = ""

    @property
    def has_marks(self) -> bool:
        return len(self.marks) > 0


class TTSError(RuntimeError):
    """TTS 调用失败。

    ``retryable`` 的划分（与渲染器同源）：
    * **可重试**：网络抖动、5xx、限流 —— 换个时刻重试有意义；
    * **不可重试**：缺密钥、密钥无效、音色不存在、文本超长 —— 重试只会烧满 attempt。
    """

    def __init__(self, message: str, *, retryable: bool, provider: str = "") -> None:
        super().__init__(message)
        self.retryable = retryable
        self.provider = provider


class TTSProvider(Protocol):
    """TTS 服务商适配器。"""

    #: 服务商标识（写进配置、日志与 sidecar）。
    name: str

    def available(self) -> tuple[bool, str]:
        """返回 (是否可用, 原因)。不可用必须给出**下一步动作**，而不只是「不行」。"""
        ...

    def synthesize(
        self,
        text: str,
        *,
        out_path: Path,
        voice: str | None = None,
        speed: float | None = None,
    ) -> SynthesisResult:
        """把一段文本合成为音频文件。"""
        ...


# ---------------------------------------------------------------------------
# 时间戳 sidecar
# ---------------------------------------------------------------------------

#: sidecar 的格式版本。Go 侧按它判断能否解析 ——
#: 未来改结构时递增，老版本 Go 遇到不认识的版本可以直接忽略（回退到按时长对齐），
#: 而不是解析出半截错误数据。
MARKS_FORMAT_VERSION = 1


def marks_sidecar_path(audio_path: str | Path) -> Path:
    """时间戳 sidecar 的路径约定：`<音频文件>.marks.json`。"""
    p = Path(audio_path)
    return p.with_name(p.name + ".marks.json")


def write_marks_sidecar(audio_path: str | Path, result: SynthesisResult) -> Path:
    """把时间戳写到音频旁边。

    写得出去就写：没有时间戳时也写一份（`marks` 为空），
    这样 Go 侧能区分「这个服务商不给时间戳」与「文件丢了」——
    两者的排查方向完全不同。
    """
    path = marks_sidecar_path(audio_path)
    payload = {
        "version": MARKS_FORMAT_VERSION,
        "provider": result.provider,
        "duration_sec": round(result.duration_sec, 4),
        "marks": [
            {
                "text": m.text,
                "start_sec": round(m.start_sec, 4),
                "duration_sec": round(m.duration_sec, 4),
            }
            for m in result.marks
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_marks_sidecar(audio_path: str | Path) -> tuple[SentenceMark, ...]:
    """读回时间戳；文件不存在或版本不认识时返回空元组（调用方回退）。"""
    path = marks_sidecar_path(audio_path)
    if not path.is_file():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    if payload.get("version") != MARKS_FORMAT_VERSION:
        return ()
    out: list[SentenceMark] = []
    for item in payload.get("marks", []):
        try:
            out.append(
                SentenceMark(
                    text=str(item["text"]),
                    start_sec=float(item["start_sec"]),
                    duration_sec=float(item["duration_sec"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(out)
