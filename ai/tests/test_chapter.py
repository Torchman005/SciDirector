"""章节模式：整章旁白时间轴、编译与浏览器预检。"""
from __future__ import annotations

import os

import pytest

from scidirector_ai.chapter import ChapterLine, compile_chapter, estimate_timing, preflight_chapter
from scidirector_ai.chapter.examples import attention
from scidirector_ai.chapter.narration import align_lines, validate_lines
from scidirector_ai.tts.base import SentenceMark
from scidirector_ai.tts.fish import marks_from_alignment


def _marks(text: str, step: float = .2, gap_after: str = "。") -> tuple[SentenceMark, ...]:
    t, out = 0.0, []
    for ch in text:
        if ch in "，。":
            t += .4 if ch in gap_after else .1
            continue
        out.append(SentenceMark(ch, t, step))
        t += step
    return tuple(out)


def test_lines_get_sentence_and_keyword_times_with_lead() -> None:
    lines = [ChapterLine("a", "小猫喝牛奶。"), ChapterLine("b", "它太烫了。")]
    timing = align_lines(lines, _marks("小猫喝牛奶。它太烫了。"), lead=1.0)
    a, b = timing["lines"]
    assert a["start"] == 1.0 and a["chars"] == "小猫喝牛奶"
    assert a["times"][3] == pytest.approx(1.6)  # "牛" 是第 4 个字
    assert b["start"] > a["end"] - 1e-6 and b["start"] == pytest.approx(1 + 1.0 + .4)
    assert timing["dur"] > b["end"]


def test_provider_text_normalization_is_aligned_not_counted() -> None:
    # 服务商把 "2017" 读成 "二零一七"：按字数切会让后面所有句子错位。
    lines = [ChapterLine("a", "2017年提出。"), ChapterLine("b", "后来很流行。")]
    timing = align_lines(lines, _marks("二零一七年提出。后来很流行。"), lead=0)
    b = timing["lines"][1]
    assert b["chars"] == "后来很流行" and b["start"] == pytest.approx(1.4 + .4)


def test_mismatched_audio_is_rejected() -> None:
    with pytest.raises(ValueError, match="对上"):
        align_lines([ChapterLine("a", "完全不同的文字内容。")], _marks("另一段毫无关系的话"), lead=0)


def test_zero_length_alignment_unit_keeps_whole_alignment() -> None:
    # 实测 Fish 长文本里会出现 start == end 的单字；此前整段时间戳因此被丢弃。
    marks = marks_from_alignment([{"text": "一", "start": 1.0, "end": 1.0}, {"text": "句", "start": 1.0, "end": 1.2}])
    assert len(marks) == 2 and marks[0].duration_sec > 0


def test_line_ids_must_be_unique() -> None:
    with pytest.raises(ValueError):
        validate_lines([ChapterLine("a", "一。"), ChapterLine("a", "二。")])


def test_compiled_chapter_embeds_runtime_timing_and_director_code() -> None:
    timing = estimate_timing(attention.LINES)
    html = compile_chapter(attention.director_js(), timing, series="S", title="T<x>",
                           highlights=attention.HIGHLIGHTS)
    assert "window.__seek" in html and "scene('qk'" in html and "__chapterStart()" in html
    assert "T&lt;x>" in html and "</script><script>" in html
    assert html.count("<script") == html.count("</script>")


@pytest.mark.skipif(not os.environ.get("SCID_CHROME"), reason="Set SCID_CHROME for real browser acceptance")
def test_attention_chapter_passes_browser_preflight(tmp_path) -> None:
    timing = estimate_timing(attention.LINES)
    html = tmp_path / "index.html"
    html.write_text(compile_chapter(attention.director_js(), timing, highlights=attention.HIGHLIGHTS), encoding="utf-8")
    assert preflight_chapter(html, timing, executable=os.environ["SCID_CHROME"]) == []
    broken = tmp_path / "broken.html"
    broken.write_text(compile_chapter("scene('a', svg => t => K('a', '不存在的词'));",
                                      estimate_timing([ChapterLine("a", "这一句。")])), encoding="utf-8")
    problems = preflight_chapter(broken, estimate_timing([ChapterLine("a", "这一句。")]),
                                 executable=os.environ["SCID_CHROME"])
    assert any("不存在的词" in p for p in problems)
