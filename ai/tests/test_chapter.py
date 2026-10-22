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


# ---------------------------------------------------------------------------
# 内容 → 脚本 → 场景代码
# ---------------------------------------------------------------------------

def _script_dict(**over) -> dict:
    base = {"title": "注意力", "highlights": {"Query": "#E8735A", "bad": "red"}, "scenes": [
        {"id": "intro", "title": "开场", "stage": "一排词卡居中，上方留出连线空间",
         "lines": [{"id": "hello", "sub": "今天讲注意力机制", "visual": "标题放大淡入", "cues": ["注意力"]},
                   {"id": "sq", "sub": "先除以 √d", "say": "先除以根号d", "visual": "金色徽标弹出", "cues": ["根号d"]}]}]}
    base.update(over)
    return base


def test_script_separates_subtitle_and_speech_and_checks_cues() -> None:
    from scidirector_ai.chapter.writer import ChapterScript
    script = ChapterScript.model_validate(_script_dict())
    assert [l.spoken for l in script.chapter_lines()] == ["今天讲注意力机制", "先除以根号d"]
    assert script.highlights == {"Query": "#E8735A"}  # 非法颜色被丢弃
    bad = _script_dict()
    bad["scenes"][0]["lines"][1]["say"] = ""  # 朗读里留着 √ 符号
    with pytest.raises(ValueError, match="符号"):
        ChapterScript.model_validate(bad)
    bad = _script_dict()
    bad["scenes"][0]["lines"][0]["cues"] = ["不存在"]
    with pytest.raises(ValueError, match="卡点词"):
        ChapterScript.model_validate(bad)


class _FakeLLM:
    """按调用顺序返回预置结果，记录提示词。"""

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def chat_json(self, system, user, schema, **kw):
        self.calls.append(user)
        reply = self.replies.pop(0)
        return schema.model_validate(reply) if isinstance(reply, dict) else reply


def test_long_script_is_sent_back_for_shortening() -> None:
    from scidirector_ai.chapter.writer import write_script
    long = _script_dict()
    long["scenes"][0]["lines"][0]["sub"] = "今天讲注意力机制" + "，展开讲" * 12
    llm = _FakeLLM([long, {"ok": True, "issues": []}, _script_dict()])
    script = write_script(llm, "讲注意力", target_sec=5)
    assert "远超目标" in llm.calls[-1] and script.scenes[0].lines[0].sub == "今天讲注意力机制"


def test_scene_problems_are_routed_to_their_scene() -> None:
    from scidirector_ai.chapter.workflow import _by_scene
    from scidirector_ai.chapter.writer import ChapterScript
    script = ChapterScript.model_validate(_script_dict())
    grouped = _by_scene(["[场景 hello] 文字重叠", "页面异常：x", "[场景 nope] y"], script)
    assert grouped["hello"] == ["[场景 hello] 文字重叠"] and len(grouped["*"]) == 2


@pytest.mark.skipif(not os.environ.get("SCID_CHROME"), reason="Set SCID_CHROME for real browser acceptance")
def test_syntax_error_in_one_scene_is_tagged_and_others_still_run(tmp_path) -> None:
    timing = estimate_timing([ChapterLine("a", "第一句。"), ChapterLine("b", "第二句。")])
    html = tmp_path / "i.html"
    html.write_text(compile_chapter([("a", "scene('a', svg => { const x = 1; const x = 2; });"),
                                     ("b", "scene('b', svg => { const g = txt(svg, 400, 400, '好'); return t => O(g, 1); });")],
                                    timing), encoding="utf-8")
    problems = preflight_chapter(html, timing, executable=os.environ["SCID_CHROME"])
    assert any(p.startswith("[场景 a]") and "脚本错误" in p for p in problems)
    assert not any(p.startswith("[场景 b]") for p in problems)


def test_hardcoded_results_in_strings_are_rejected() -> None:
    from scidirector_ai.chapter.writer import static_problems
    assert static_problems("txt(svg, 1, 1, pct(w[3])); S('rect', {width: '100%'})", "a") == []
    problems = static_problems("txt(svg, 1, 1, '43%'); txt(svg, 1, 1, 'Σ = 23')", "a")
    assert problems and problems[0].startswith("[场景 a]") and "43%" in problems[0]


def test_script_data_keys_are_identifiers() -> None:
    from scidirector_ai.chapter.writer import ChapterScript
    ok = ChapterScript.model_validate(_script_dict(data={"scores": [1, 2], "d": 64}))
    assert ok.data["d"] == 64
    with pytest.raises(ValueError, match="标识符"):
        ChapterScript.model_validate(_script_dict(data={"分数": [1]}))


@pytest.mark.skipif(not os.environ.get("SCID_CHROME"), reason="Set SCID_CHROME for real browser acceptance")
def test_shape_painted_over_text_is_reported_but_backing_panel_is_not(tmp_path) -> None:
    timing = estimate_timing([ChapterLine("a", "第一句话比较长一些。")])
    code = ("scene('a', svg => { S('rect', {x: 100, y: 300, width: 600, height: 200, fill: C.card}, svg);"
            " txt(svg, 400, 400, '被遮住的标签', {'font-size': 40});"
            " S('rect', {x: 380, y: 370, width: 300, height: 60, fill: C.q}, svg);"
            " txt(svg, 400, 600, '背景板上的字', {'font-size': 40}); });")
    html = tmp_path / "i.html"
    html.write_text(compile_chapter([("a", code)], timing, data={"scores": [1]}), encoding="utf-8")
    problems = preflight_chapter(html, timing, executable=os.environ["SCID_CHROME"])
    covered = [p for p in problems if "图形" in p]
    assert len(covered) == 1 and "被遮住的标签" in covered[0]


def test_static_checker_reads_real_string_literals_and_shadowing() -> None:
    from scidirector_ai.chapter.writer import static_problems
    # 实测误报：把两个字面量之间的代码当成了字符串。
    code = "txt(g, 1, 1, l, {'font-weight': 700, fill: j === 5 ? 'a' : 'b'}); txt(g, 1, 1, `${pct(x)} = ${y}`);"
    assert static_problems(code, "a") == []
    assert "同名" in static_problems("const bars = 1;", "a")[0]
