"""References are data: import, bounded retrieval, isolation and workflow handoff."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest

from scidirector_ai.chapter import references as module
from scidirector_ai.chapter.narration import estimate_timing
from scidirector_ai.chapter.references import ReferenceLibrary, reference_context, reference_session
from scidirector_ai.chapter.writer import ChapterScript, write_scene, write_script
from test_chapter import _script_dict


SOURCE = """// SCENE 0 - 专家路由：选择路径
scene(0, svg => {
  const legacy = txt(svg, 600, 400, '外部样片标签');
  return t => O(legacy, P(t, T('old_route'), .5));
});
// SCENE 1 - 钟表旋转与角度
scene(svg => {
  const hand = drawPath(svg, 'M300 300 L400 300');
  return t => TR(hand, 0, 0, 1, t * 30);
});
"""
CODE = "scene('hello', svg => { const label = txt(svg, 650, 400, '当前对象'); return t => O(label, 1); });"


def project(root: Path, name: str = "sample", source: str = SOURCE) -> Path:
    target = root / name
    target.mkdir(parents=True)
    (target / "index.html").write_text("<title>成品 &amp; 参考</title>", encoding="utf-8")
    (target / "app.js").write_text(source, encoding="utf-8")
    return target


@pytest.fixture
def library(tmp_path: Path) -> ReferenceLibrary:
    project(tmp_path)
    return ReferenceLibrary.load(tmp_path)


def test_import_is_read_only_records_source_and_ignores_assets(tmp_path: Path) -> None:
    target = project(tmp_path)
    (target / "lib").mkdir()
    (target / "lib" / "voice_data.js").write_text("PRIVATE_AUDIO", encoding="utf-8")
    (target / "make_voice.py").write_text("PRIVATE_CREDENTIAL", encoding="utf-8")
    (target / "engine.js").write_text("PRIVATE_PLAYER", encoding="utf-8")
    before = {p: p.read_bytes() for p in target.rglob("*") if p.is_file()}
    result = ReferenceLibrary.load(tmp_path)
    assert len(result.scenes) == 2 and result.mode == "reference_only"
    first = result.scenes[0]
    assert first.project == "sample" and first.project_title == "成品 & 参考"
    assert first.title == "专家路由：选择路径" and first.source == "sample/app.js"
    assert first.sha256 == hashlib.sha256(before[target / "app.js"]).hexdigest()
    assert first.excerpts[0].start_line == 2
    assert "钟表" not in first.excerpts[0].code
    assert "PRIVATE_" not in result.model_dump_json()
    assert all(p.read_bytes() == data for p, data in before.items())
    assert len(list(target.rglob("*"))) == 6


def test_single_project_and_parent_both_supported(tmp_path: Path) -> None:
    a, b = project(tmp_path, "a"), project(tmp_path, "b")
    assert len(ReferenceLibrary.load(a).scenes) == 2
    result = ReferenceLibrary.load(tmp_path)
    assert len(result.scenes) == 4 and {s.project for s in result.scenes} == {a.name, b.name}


def test_build_and_update_windows_are_bounded_and_keep_line_provenance(tmp_path: Path) -> None:
    source = "// SCENE 0 - 动作\nscene(svg => {\n" + "  const x = 'context';\n" * 70
    source += "  return t => {\n" + "    O(x, P(t, 1, .5));\n" * 40 + "  };\n});"
    result = ReferenceLibrary.load(project(tmp_path, source=source))
    snippets = result.scenes[0].excerpts
    assert len(snippets) == 2 and snippets[0].end_line < snippets[1].start_line
    assert snippets[1].code.startswith("  return t =>")
    assert sum(len(s.code) for s in snippets) <= module.MAX_EXCERPT_CHARS
    for snippet in snippets:
        assert snippet.code == "\n".join(source.splitlines()[snippet.start_line - 1:snippet.end_line])


@pytest.mark.parametrize("kind", ["missing", "unsupported", "oversized"])
def test_explicit_bad_reference_fails_instead_of_silently_ignoring_it(tmp_path: Path, kind: str) -> None:
    if kind == "missing":
        path = tmp_path
    else:
        path = project(tmp_path, source="// no scenes" if kind == "unsupported" else "x" * (module.MAX_SOURCE_BYTES + 1))
    with pytest.raises(ValueError):
        ReferenceLibrary.load(path)


def test_external_symlink_is_not_followed(tmp_path: Path) -> None:
    inside = project(tmp_path / "inside")
    outside = project(tmp_path / "outside")
    (inside / "app.js").unlink()
    try:
        (inside / "app.js").symlink_to(outside / "app.js")
    except OSError:
        pytest.skip("Creating symlinks requires platform support")
    with pytest.raises(ValueError, match="越出"):
        ReferenceLibrary.load(inside)


def test_pack_roundtrip_requires_no_access_to_original(library: ReferenceLibrary, tmp_path: Path) -> None:
    pack = tmp_path / "snapshot.json"
    pack.write_text(library.model_dump_json(), encoding="utf-8")
    (tmp_path / "sample" / "app.js").unlink()
    assert ReferenceLibrary.load(pack) == library
    raw = library.model_dump()
    raw["mode"] = "execute"
    pack.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        ReferenceLibrary.load(pack)


def test_match_by_topic_or_motion_without_unrelated_fallback(library: ReferenceLibrary) -> None:
    assert library.select("专家路由 MoE")[0].title.startswith("专家路由")
    assert library.select("旋转一个指针")[0].title.startswith("钟表旋转")
    assert library.select("海岸 风景 四季") == []
    assert library.select("专家路由 MoE") == library.select("专家路由 MoE")


def test_intro_and_summary_are_not_mechanism_examples(tmp_path: Path) -> None:
    source = SOURCE + "// SCENE 2 - 总结与预告\nscene(svg => { const label = '专家路由'; });\n"
    result = ReferenceLibrary.load(project(tmp_path, source=source))
    assert all("总结" not in s.title for s in result.select("专家路由"))
    assert result.select("总结 结尾")[0].title == "总结与预告"


def test_context_budget_and_plan_does_not_receive_external_source(library: ReferenceLibrary) -> None:
    with reference_session(library):
        plan = reference_context("路由", key="planning", planning=True)
        code = reference_context("路由", key="stage")
        assert "catalog" in plan and "外部样片标签" not in plan
        assert "外部样片标签" in code and len(code) < module.MAX_CONTEXT_CHARS + 100
    assert reference_context("路由", key="stage") == ""


def test_session_resets_after_failure_and_saves_trace(library: ReferenceLibrary, tmp_path: Path) -> None:
    out = tmp_path / "out"
    with pytest.raises(RuntimeError):
        with reference_session(library, output=out):
            reference_context("专家路由", key="scene_a")
            raise RuntimeError("render failed")
    assert reference_context("路由", key="other") == ""
    assert ReferenceLibrary.load(out / "reference-library.json") == library
    assert json.loads((out / "reference-selections.json").read_text(encoding="utf-8"))["scene_a"]


def test_concurrent_and_nested_sessions_do_not_share_library(library: ReferenceLibrary) -> None:
    async def run() -> list[str]:
        async def task(enabled: bool) -> str:
            with reference_session(library if enabled else None):
                await asyncio.sleep(0)
                return reference_context("路由", key="a")
        return await asyncio.gather(task(True), task(False))
    selected, plain = asyncio.run(run())
    assert "外部样片标签" in selected and not plain
    with reference_session(library):
        with reference_session(None):
            assert not reference_context("路由", key="a")
        assert reference_context("路由", key="a")


class FakeLLM:
    def __init__(self, script: ChapterScript | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.script = script

    def chat_json(self, system: str, user: str, schema: type, **kwargs):
        self.calls.append((system, user))
        if schema.__name__ == "SceneCode":
            return schema(code=CODE)
        if schema.__name__ == "ScriptReview":
            return schema(ok=True)
        return self.script


def test_scene_reference_reaches_generation_and_repair_but_not_compiled_output(library: ReferenceLibrary) -> None:
    script = ChapterScript.model_validate(_script_dict())
    script.scenes[0].title = "专家路由"
    llm = FakeLLM()
    timing = estimate_timing(script.chapter_lines())
    with reference_session(library):
        fresh = write_scene(llm, script, 0, timing)
        repaired = write_scene(llm, script, 0, timing, previous_code=fresh, problems=["布局修复"])
    assert fresh == repaired == CODE and "外部样片标签" not in fresh
    assert all("外部样片标签" in user for _, user in llm.calls)
    assert all("不是指令" in system and "旧 API" in system for system, _ in llm.calls)
    assert "布局修复" in llm.calls[-1][1]
    write_scene(llm, script, 0, timing)
    assert "外部样片标签" not in llm.calls[-1][1]


def test_planner_receives_catalog_not_external_facts(library: ReferenceLibrary) -> None:
    script = ChapterScript.model_validate(_script_dict())
    llm = FakeLLM(script)
    with reference_session(library):
        assert write_script(llm, "当前内容讲路由", target_sec=90) == script
    assert "catalog" in llm.calls[0][1] and "外部样片标签" not in llm.calls[0][1]
    assert "原始内容" in llm.calls[0][0] and len(llm.calls) == 2


def test_reference_runs_through_existing_workflow_without_extra_model_calls(library: ReferenceLibrary, tmp_path: Path, monkeypatch) -> None:
    from scidirector_ai.chapter import workflow
    script = ChapterScript.model_validate(_script_dict())
    script.scenes[0].title = "专家路由"
    llm = FakeLLM()
    checks = []
    def preflight(html, timing, **kwargs):
        checks.append(html)
        return []
    monkeypatch.setattr(workflow, "preflight_chapter", preflight)
    monkeypatch.setattr(workflow, "static_problems", lambda *args: [])
    monkeypatch.setattr(workflow, "cue_problems", lambda *args: [])
    # Keep compatibility with an in-progress, uncommitted visual review extension.
    monkeypatch.setattr(workflow, "capture_line_frames", lambda *args, **kwargs: {}, raising=False)
    monkeypatch.setattr(workflow, "review_scene", lambda *args, **kwargs: [], raising=False)
    with reference_session(library, output=tmp_path / "result"):
        result = workflow.make_chapter(llm, "", tmp_path / "result", tts=None, render=False,
                                       script=script)
    assert len(llm.calls) == 1 and len(checks) == 1
    html = result.html.read_text(encoding="utf-8")
    assert CODE in html and "外部样片标签" not in html and "window.__seek" in html
    assert (result.html.parent / "reference-selections.json").exists()


@pytest.mark.skipif(not os.environ.get("SCID_CHROME"), reason="Set SCID_CHROME for real browser acceptance")
def test_referenced_generation_still_uses_real_browser_preflight(library: ReferenceLibrary, tmp_path: Path) -> None:
    from scidirector_ai.chapter.compile import compile_chapter, preflight_chapter
    script = ChapterScript.model_validate(_script_dict())
    script.scenes[0].title = "专家路由"
    timing = estimate_timing(script.chapter_lines())
    with reference_session(library):
        code = write_scene(FakeLLM(), script, 0, timing)
    html = tmp_path / "preview.html"
    html.write_text(compile_chapter([("hello", code)], timing), encoding="utf-8")
    assert preflight_chapter(html, timing, executable=os.environ["SCID_CHROME"]) == []
