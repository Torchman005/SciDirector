"""Scene compiler, bounded generation and actual trusted JS seek behavior."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from scidirector_ai.agents.coder import CoderAgent, _RawCode
from scidirector_ai.config import Settings
from scidirector_ai.generation_quality import QualityResult
from scidirector_ai.llm import LLMClient, LLMParseError, Task
from scidirector_ai.scene import SceneSpec, compile_scene, extract_scene, scene_runtime
from scidirector_ai.schemas import RenderEngine, SceneTag, ShotSpec, StyleGuide
from test_coder import VALID_HTML, VALID_MANIM


def spec(**changes: Any) -> SceneSpec:
    el = {"id": "headline", "kind": "card", "box": {"x": .08, "y": .08, "width": .84, "height": .3},
          "text": "首稿清晰，修改可定位", "font_size": 48}
    el.update(changes)
    return SceneSpec.model_validate({"elements": [el], "explanation": "阶段说明"})


def compile_it(scene: SceneSpec) -> str:
    return compile_scene(scene, width=1920, height=1080, duration=8, style=StyleGuide())


@pytest.mark.parametrize("change", [
    {"box": {"x": .8, "y": .2, "width": .4, "height": .2}},
    {"text": ""}, {"kind": "script"}, {"color": "url(https://example.com)"},
    {"html": "<script>alert(1)</script>"},
    {"keyframes": [{"time": .2}]},
    {"keyframes": [{"time": 0}, {"time": .8}, {"time": .8}]},
    {"keyframes": [{"time": 0, "dx": float("nan")}]},
    {"keyframes": [{"time": 0, "rotation": 45}]},
    {"keyframes": [{"time": 0, "reveal": 0}]},
    {"kind": "bars", "data": [{"label": "测试", "value": -1}], "unit": "%"},
    {"kind": "bars", "data": [{"label": "测试", "value": 1}], "unit": ""},
])
def test_schema_rejects_invalid_or_unsupported_input(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        spec(**change)


def test_ids_are_unique() -> None:
    el = spec().elements[0].model_dump()
    with pytest.raises(ValidationError, match="唯一"):
        SceneSpec(elements=[el, el])


@pytest.mark.parametrize("change,needle", [
    ({"font_size": 24}, "字号"),
    ({"keyframes": [{"time": 0}, {"time": .8, "dx": -.2}]}, "超出画布"),
    ({"keyframes": [{"time": 0}, {"time": .8, "dx": .2}]}, "超出画布"),
    ({"keyframes": [{"time": 0, "opacity": 0}, {"time": .9, "opacity": 0}]}, "末帧"),
    ({"box": {"x": .08, "y": .08, "width": .08, "height": .05}}, "区域过小"),
])
def test_production_validation_blocks_defects(change: dict[str, Any], needle: str) -> None:
    with pytest.raises(ValueError, match=needle):
        compile_it(spec(**change))


def test_compilation_is_deterministic_safe_and_revision_preserves_ir() -> None:
    scene = spec(text='</script><script>missingSymbol()</script> & <b>字面文字</b>')
    code = compile_it(scene)
    assert code == compile_it(scene)
    assert '</script><script>missingSymbol()' not in code
    assert extract_scene(code) == scene
    assert extract_scene(VALID_HTML) is None


def test_opening_fade_in_end_keyframe_and_near_edge_text_are_renderable() -> None:
    scene = spec(box={"x": .04, "y": .04, "width": .9, "height": .3},
                 keyframes=[{"time": 0, "opacity": 0, "dx": -.1},
                            {"time": .15, "opacity": 1}, {"time": 1, "opacity": 1}])
    html = compile_it(scene)
    assert extract_scene(html) == scene
    assert '"animatedUntil": 0.9375' in html


def test_background_presets_follow_user_selection_and_auto_scene_choice() -> None:
    scene = spec()
    scene.background = "gradient"
    auto = compile_it(scene)
    forced = compile_scene(scene, width=1920, height=1080, duration=8,
                           style=StyleGuide(background_style="grid"))
    assert "135deg" in auto and "background-size: 64px 64px" in forced
    assert extract_scene(forced) == scene


class Checker:
    def __init__(self, issues: list[list[str]] | None = None) -> None:
        self.issues = issues or [[]]
        self.calls: list[str] = []

    def check(self, code: str, shot: ShotSpec, style: StyleGuide) -> QualityResult:
        self.calls.append(code)
        return QualityResult(checked=True, issues=self.issues.pop(0))


class Stub:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def chat_json(self, system: str, user: str, schema: type, **kw: Any) -> Any:
        self.calls.append({"system": system, "user": user, "schema": schema, **kw})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.parametrize("engine", [RenderEngine.D3, RenderEngine.ECHARTS, RenderEngine.CODE_ANIM, RenderEngine.MOTION])
def test_html_engines_default_to_scene_with_original_contract(engine: RenderEngine) -> None:
    settings = Settings(env="test", llm_provider="mock")
    llm, checker = Stub([spec()]), Checker()
    result = CoderAgent(llm, settings, quality_checker=checker).generate(
        shot=ShotSpec(engine=engine), style_guide=StyleGuide())
    assert result.policy_ok and result.llm_attempts == 1 and result.artifact.language == "html+js"
    assert extract_scene(result.code) == spec()
    assert llm.calls[0]["task"] == Task.SCENE and llm.calls[0]["max_parse_attempts"] == 1
    assert "JSON Schema" in llm.calls[0]["system"] and not result.examples_used
    assert "{{" not in llm.calls[0]["system"] and "{{" not in llm.calls[0]["user"]


def test_revision_sends_ir_and_feedback_without_runtime() -> None:
    previous = compile_it(spec())
    llm = Stub([spec(text="清晰的修订")])
    agent = CoderAgent(llm, Settings(env="test", llm_provider="mock"), quality_checker=Checker())
    result = agent.generate(shot=ShotSpec(tag=SceneTag.MOTION), style_guide=StyleGuide(), attempt=2,
                            feedback_text="R1 放大 headline", previous_code=previous)
    user = llm.calls[0]["user"]
    assert "headline" in user and "R1" in user and "首稿清晰" in user
    assert "document.createElement" not in user and "scid-scene-v1" not in user
    assert '"dx":0' not in user and '"data":[]' not in user
    assert extract_scene(result.code).elements[0].text == "清晰的修订"


def test_scene_uses_independent_configured_output_budget() -> None:
    llm = Stub([spec()])
    settings = Settings(env="test", llm_provider="mock", llm_max_tokens=4096, coder_scene_max_tokens=7000)
    result = CoderAgent(llm, settings, quality_checker=Checker()).generate(
        shot=ShotSpec(tag=SceneTag.MOTION), style_guide=StyleGuide())
    assert result.policy_ok and llm.calls[0]["max_tokens"] == 7000


def test_schema_layout_and_browser_share_one_repair_budget() -> None:
    llm = Stub([LLMParseError("id 重复", raw_response='{"elements":[]}'), spec()])
    checker = Checker([["正文裁切"]])
    agent = CoderAgent(llm, Settings(env="test", llm_provider="mock"), quality_checker=checker)
    result = agent.generate(shot=ShotSpec(tag=SceneTag.MOTION), style_guide=StyleGuide())
    assert not result.policy_ok and result.llm_attempts == 2
    assert len(llm.calls) == 2 and len(checker.calls) == 1
    assert "id 重复" in llm.calls[1]["user"] and '{"elements":[]}' in llm.calls[1]["user"]
    assert all(c["task"] == Task.SCENE for c in llm.calls)
    assert "场景生成已尝试 2 次" in result.policy_summary


def test_legacy_html_envelope_from_scene_provider_still_renders() -> None:
    from scidirector_ai.llm import LLMParseError

    legacy = '{"code":"<script>window.__seek=t=>{};window.__ready=true;</script>","language":"html+js","explanation":"legacy"}'

    class Legacy:
        def chat_json(self, system: str, user: str, schema: type, **kw: Any) -> Any:
            raise LLMParseError("结构不符", raw_response=legacy)

    result = CoderAgent(Legacy(), Settings(env="test", llm_provider="mock"), quality_checker=Checker()).generate(
        shot=ShotSpec(tag=SceneTag.MOTION), style_guide=StyleGuide())
    assert result.policy_ok and result.code.startswith("<script>") and result.llm_attempts == 1


def test_client_scene_parse_does_not_add_a_hidden_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    client = LLMClient(Settings(env="test", llm_provider="mock"))
    calls: list[dict[str, Any]] = []
    def call(**kw: Any) -> str:
        calls.append(kw)
        return '{"version":1,"elements":[]}'
    monkeypatch.setattr(client, "_call", call)
    with pytest.raises(LLMParseError) as error:
        client.chat_json("schema", "生成场景", SceneSpec, task=Task.SCENE, max_parse_attempts=1)
    assert len(calls) == 1 and error.value.raw_response == '{"version":1,"elements":[]}'
    with pytest.raises(LLMParseError):
        client.chat_json("schema", "生成场景", SceneSpec, task=Task.SCENE)
    assert len(calls) == 3


def test_layout_is_repaired_before_browser_and_browser_issues_are_repaired() -> None:
    for bad, checker in [(spec(font_size=24), Checker()), (spec(), Checker([["R1 正文裁切"], []]))]:
        llm = Stub([bad, spec(text="修好了")])
        result = CoderAgent(llm, Settings(env="test", llm_provider="mock"), quality_checker=checker).generate(
            shot=ShotSpec(tag=SceneTag.MOTION), style_guide=StyleGuide())
        assert result.policy_ok and result.llm_attempts == 2
        assert "生成内修复" in llm.calls[1]["user"]


@pytest.mark.parametrize("mode,previous", [("code", ""), ("structured", VALID_HTML)])
def test_explicit_code_and_historical_html_remain_compatible(mode: str, previous: str) -> None:
    llm = Stub([_RawCode(code=VALID_HTML)])
    result = CoderAgent(llm, Settings(env="test", llm_provider="mock", coder_scene_mode=mode), quality_checker=Checker()).generate(
        shot=ShotSpec(tag=SceneTag.MOTION), style_guide=StyleGuide(), previous_code=previous)
    assert result.code == VALID_HTML and llm.calls[0]["task"] == Task.CODE


def test_per_shot_escape_and_manim_stay_on_code_path() -> None:
    for shot, code in [(ShotSpec(tag=SceneTag.MOTION, meta={"generation_mode": "code"}), VALID_HTML),
                       (ShotSpec(tag=SceneTag.MATH), VALID_MANIM)]:
        llm = Stub([_RawCode(code=code)])
        result = CoderAgent(llm, Settings(env="test", llm_provider="mock"), quality_checker=Checker()).generate(
            shot=shot, style_guide=StyleGuide())
        assert result.policy_ok and result.code == code and llm.calls[0]["task"] == Task.CODE


def test_offline_model_returns_real_scene_and_valid_manim() -> None:
    settings = Settings(env="test", llm_provider="mock")
    agent = CoderAgent(LLMClient(settings), settings)
    assert extract_scene(agent.generate(shot=ShotSpec(tag=SceneTag.DATA), style_guide=StyleGuide()).code)
    assert agent.generate(shot=ShotSpec(tag=SceneTag.MATH), style_guide=StyleGuide()).policy_ok


def test_runtime_executes_repeat_reverse_and_parallel_chunk_seeks(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node 不可用；真实浏览器对照另行执行")
    scene = spec(kind="code", text="中文🙂 ABC", keyframes=[{"time": 0, "reveal": 0}, {"time": .8, "reveal": 1, "dx": .01}])
    bars = spec(kind="bars", data=[{"label": "组A", "value": 30}, {"label": "组B", "value": 50}],
                unit="%", color="primary", box={"x": .08, "y": .45, "width": .84, "height": .4},
                keyframes=[{"time": 0, "reveal": 0}, {"time": .8, "reveal": 1}]).elements[0]
    bars.id = "comparison"
    scene.elements.append(bars)
    scene.elements[0].keyframes[-1].time = 1
    payload = {"scene": scene.model_dump(), "width": 1920, "height": 1080, "duration": 8, "animatedUntil": .9375,
               "style": {"theme": "dark", "primary": "#4F8CFF", "background": "#0B1020", "font": "Microsoft YaHei"}}
    harness = """
const vm=require('node:vm'), assert=require('node:assert/strict');
const runtime=RUNTIME, payload=PAYLOAD;
function context() {
  class El {
    constructor(tag){this.tag=tag;this.children=[];this.style={};this.attrs={};this.textContent='';}
    appendChild(n){this.children.push(n);}
    setAttribute(k,v){this.attrs[k]=v;}
  }
  const stage=new El('div'), data=new El('script');data.textContent=JSON.stringify(payload);
  const document={body:new El('body'),getElementById:id=>id==='stage'?stage:data,
    createElement:t=>new El(t),createElementNS:(_,t)=>new El(t)};
  const ctx={document,window:{},innerWidth:960,innerHeight:540};
  vm.runInNewContext(runtime,ctx);return {ctx,stage};
}
const {ctx,stage}=context();
function state(){return JSON.stringify(stage);}
ctx.window.__seek(4);const middle=state();
ctx.window.__seek(7.9);const end=state();assert.notEqual(middle,end);
ctx.window.__seek(7.5);assert.equal(state(),end);
ctx.window.__seek(8);assert.equal(state(),end);
ctx.window.__seek(4);assert.equal(state(),middle);
ctx.window.__seek(4);assert.equal(state(),middle);
ctx.window.__seek(0);assert.equal(stage.children[0].children[0].textContent,'');
ctx.window.__seek(7.9);assert.equal(stage.children[0].children[0].textContent,'中文🙂 ABC');
assert.equal(stage.style.transform,'scale(0.5,0.5)');
const other=context();other.ctx.window.__seek(4);assert.equal(JSON.stringify(other.stage),middle);
assert.equal(stage.children.length,2);
console.log('seek determinism, Unicode typing, bar growth, draft scaling: passed');
""".replace("RUNTIME", json.dumps(scene_runtime())).replace("PAYLOAD", json.dumps(payload, ensure_ascii=False))
    script = tmp_path / "runtime.cjs"
    script.write_text(harness, encoding="utf-8")
    proc = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.skipif(not os.environ.get("SCID_CHROME"), reason="SCID_CHROME 未设置")
@pytest.mark.parametrize("kind", ["text", "card", "code", "bars"])
def test_scene_in_real_browser(tmp_path: Path, kind: str) -> None:
    from scidirector_ai.browser_preflight import inspect
    page = tmp_path / "index.html"
    changes: dict[str, Any] = {"kind": kind}
    if kind == "code":
        changes.update(text="def seek(t):\n    return t * 2", keyframes=[{"time": 0, "reveal": 0}, {"time": .85, "reveal": 1}])
    elif kind == "bars":
        changes.update(box={"x": .08, "y": .25, "width": .84, "height": .55}, color="primary",
                       unit="%", data=[{"label": "实验组", "value": 92}, {"label": "对照组", "value": 61}],
                       keyframes=[{"time": 0, "reveal": 0}, {"time": .85, "reveal": 1}])
    scene = spec(**changes)
    if kind in {"code", "bars"}:
        scene.elements[0].box.y = .3
        title = spec(kind="text", text="逐步呈现", box={"x": .08, "y": .08, "width": .84, "height": .16}).elements[0]
        title.id = "title"
        scene.elements.append(title)
    page.write_text(compile_it(scene), encoding="utf-8")
    report = inspect(page, 1920, 1080, 8, 32, os.environ["SCID_CHROME"])
    assert not report["issues"], report


@pytest.mark.skipif(not os.environ.get("SCID_CHROME"), reason="SCID_CHROME 未设置")
def test_actual_browser_catches_overlong_scene_text(tmp_path: Path) -> None:
    from scidirector_ai.browser_preflight import inspect
    page = tmp_path / "index.html"
    page.write_text(compile_it(spec(text="超长文字需要拆分阶段。" * 100)), encoding="utf-8")
    report = inspect(page, 1920, 1080, 8, 32, os.environ["SCID_CHROME"])
    assert any("裁切" in issue for issue in report["issues"]), report


@pytest.mark.skipif(not os.environ.get("SCID_CHROME") or not shutil.which("ffmpeg"),
                    reason="真实出片测试需要 SCID_CHROME 与 ffmpeg")
def test_opening_fade_and_end_keyframe_produce_real_mp4(tmp_path: Path) -> None:
    from scidirector_ai.renderer import HtmlRenderer, RenderRequest
    from scidirector_ai.sandbox.runner import SandboxRunner

    settings = Settings(env="test", llm_provider="mock", sandbox_timeout_sec=90)
    scene = spec(kind="text", box={"x": .04, "y": .06, "width": .9, "height": .3},
                 keyframes=[{"time": 0, "opacity": 0}, {"time": .2, "opacity": 1},
                            {"time": 1, "opacity": 1}])
    html = compile_scene(scene, width=1920, height=1080, duration=3, style=StyleGuide())
    renderer = HtmlRenderer(settings, "motion")
    result = renderer.render(RenderRequest(shot_id="scene-regression", code=html, output_dir=tmp_path,
                            duration_sec=3, width=1920, height=1080, fps=10), SandboxRunner())
    assert Path(result.video_path).stat().st_size > 1000
    assert abs(result.duration_sec - 3) < .15
    assert (result.width, result.height) == (1920, 1080)
