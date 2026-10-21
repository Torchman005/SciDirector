"""连读合成：相邻镜头作为语调上下文，再按逐字时间戳裁回本镜头。"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from scidirector_ai.tts.base import SentenceMark, SynthesisResult
from scidirector_ai.tts.context import _target_span, context_parts, synthesize_in_context

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要 ffmpeg")

CHAR_SEC = 0.2


class FakeFish:
    """按字均匀给出时间戳；标点处插入 0.4 秒停顿，与真实服务的形状一致。"""

    name = "fish"

    def __init__(self) -> None:
        self.texts: list[str] = []

    def synthesize(self, text, *, out_path, voice=None, speed=None):
        self.texts.append(text)
        marks, t = [], 0.0
        for ch in text:
            if ch in "，。！？":
                t += 0.4
                continue
            marks.append(SentenceMark(text=ch, start_sec=t, duration_sec=CHAR_SEC))
            t += CHAR_SEC
        out = Path(out_path)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=f=220:d={t + 0.3:.2f}",
                        "-c:a", "libmp3lame", str(out)], check=True)
        return SynthesisResult(audio_path=out, duration_sec=t, marks=tuple(marks), provider="fish")


def _duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True, check=True)
    return float(out.stdout)


def test_context_parts_take_adjacent_sentences() -> None:
    assert context_parts("甲说完了。乙接着说。", "本句。", "丙开头。丁结尾。", 1) == ("乙接着说。", "丙开头。")
    assert context_parts("甲。", "本句。", "丙。", 0) == ("", "")


def test_shot_audio_is_cut_from_context_and_marks_are_shifted(tmp_path: Path) -> None:
    provider = FakeFish()
    out = tmp_path / "narration.mp3"
    result = synthesize_in_context(provider, "门控让信号更平滑。", out_path=out,
                                   prev_text="先看传统激活。", next_text="再看专家混合。")
    assert provider.texts == ["先看传统激活。门控让信号更平滑。再看专家混合。"]
    assert "".join(m.text for m in result.marks) == "门控让信号更平滑"
    assert result.marks[0].start_sec < 0.4  # 只保留本镜头前的半段停顿
    assert abs(_duration(out) - (result.marks[-1].end_sec + 0.2)) < 0.25
    assert not (tmp_path / "narration.context.mp3").exists()


def test_mismatched_alignment_falls_back_to_plain_synthesis(tmp_path: Path) -> None:
    marks = (SentenceMark("AB", 0, .2), SentenceMark("C", .2, .2))
    assert _target_span(marks, "A", "BC") is None  # 对齐单元跨越镜头边界

    provider = FakeFish()
    result = synthesize_in_context(provider, "本句。", out_path=tmp_path / "n.mp3", prev_text="", next_text="")
    assert provider.texts == ["本句。"] and result.marks[0].start_sec == 0
