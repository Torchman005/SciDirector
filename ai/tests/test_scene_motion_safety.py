"""Regression controls for observed production overlaps and timeline drift."""
import os
from pathlib import Path

import pytest

from scidirector_ai.browser_preflight import inspect, sample_times
from scidirector_ai.scene import SceneSpec, compile_scene
from scidirector_ai.schemas import StyleGuide


def element(id, kind="rect", x=.1, **kw):
    return dict(id=id, kind=kind, box=dict(x=x,y=.3,width=.3,height=.25),
                **({"text":id,"font_size":32} if kind=="text" else {}), **kw)


def test_explicit_line_normalizes_and_sampling_includes_motion_midpoint():
    scene=SceneSpec(elements=[element("path",kind="line",points=[{"x":0,"y":0},{"x":1,"y":1}],
        keyframes=[{"time":.32},{"time":.4}])])
    assert scene.elements[0].kind=="polyline"
    assert any(t==pytest.approx(3.6) for t in sample_times(10,{"scene":scene.model_dump(),"animatedUntil":.95}))


def grouped_scene() -> SceneSpec:
    return SceneSpec.model_validate({"elements": [
        element("person", kind="rect", x=.1, motion_group="story", color="secondary"),
        element("prop", kind="rect", x=.45, motion_group="story", color="accent"),
    ], "motion_groups": [{"id": "story", "keyframes": [
        {"time": 0}, {"time": .5, "dx": .1, "scale": 1.1, "easing": "ease_out"},
    ]}]})


def test_group_requires_existing_track_and_multiple_members():
    scene = grouped_scene()
    payload = scene.model_dump()
    payload["motion_groups"] = []
    with pytest.raises(ValueError, match="不存在的运动组"):
        SceneSpec.model_validate(payload)
    payload = scene.model_dump()
    payload["elements"][1]["motion_group"] = ""
    with pytest.raises(ValueError, match="至少需要两个元素"):
        SceneSpec.model_validate(payload)
    payload = scene.model_dump()
    payload["motion_groups"][0]["keyframes"] = [{"time": .5}, {"time": .5}]
    with pytest.raises(ValueError, match="严格递增"):
        SceneSpec.model_validate(payload)
    scene.motion_groups[0].keyframes[-1].dx = .25
    with pytest.raises(ValueError, match="超出画布"):
        compile_scene(scene, width=1280, height=720, duration=4, style=StyleGuide())


def test_group_keyframes_are_sampled_for_mid_motion():
    scene = grouped_scene()
    payload = {"scene": scene.model_dump(), "animatedUntil": .95}
    assert any(t == pytest.approx(1.) for t in sample_times(4, payload))


@pytest.mark.skipif(not os.environ.get("SCID_CHROME"),reason="需要 Chromium")
@pytest.mark.parametrize("elements,needle",[
    ([element("a"),element("b",x=.2)],"同色图形重叠"),
    ([element("a","text"),element("b","text")],"文字重叠"),
    ([element("label","text"),element("cover")],"遮挡"),
    ([element("shape"),element("label","text")],""),
    ([element("shape",x=.2),element("label","text",layer=5,align="center")],"遮挡"),
    ([element("shape",x=.2,overlap_group="labelled"),element("label","text",layer=5,align="center",overlap_group="labelled")],""),
    ([element("a",overlap_group="compound"),element("b",x=.2,overlap_group="compound")],""),
])
def test_layout_issues_trigger_repair_without_rejecting_labels(tmp_path: Path,elements,needle):
    file=tmp_path/"index.html"
    file.write_text(compile_scene(SceneSpec(elements=elements),width=1280,height=720,duration=4,style=StyleGuide()),encoding="utf-8")
    report=inspect(file,1280,720,4,32,os.environ["SCID_CHROME"])
    if needle:
        assert any(needle in issue for issue in report["issues"]),report
    else:
        assert not report["issues"],report


@pytest.mark.skipif(not os.environ.get("SCID_CHROME"),reason="需要 Chromium")
def test_delayed_entry_and_shared_timeline_are_seek_invariant(tmp_path: Path):
    from playwright.sync_api import sync_playwright
    scene=SceneSpec(elements=[
        element("a",keyframes=[{"time":0},{"time":.5,"dx":.2}]),
        element("b",keyframes=[{"time":0},{"time":.5,"dx":.2},{"time":1,"dx":.2}]),
        element("late",keyframes=[{"time":.3}]),
    ])
    file=tmp_path/"index.html"
    file.write_text(compile_scene(scene,width=1280,height=720,duration=4,style=StyleGuide()),encoding="utf-8")
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=os.environ["SCID_CHROME"])
        page=browser.new_page(viewport={"width":1280,"height":720})
        page.goto(file.as_uri())
        for t in [.8,3.8,.8]:
            page.evaluate("t=>window.__seek(t)",t)
            a,b=page.locator("#a,#b").evaluate_all("es=>es.map(e=>e.style.transform)")
            assert a==b
            assert page.locator("#late").evaluate("e=>e.style.opacity")==("0" if t==.8 else "1")
        browser.close()


@pytest.mark.skipif(not os.environ.get("SCID_CHROME"), reason="需要 Chromium")
def test_group_motion_keeps_members_together_and_catches_mid_motion_overlap(tmp_path: Path):
    from playwright.sync_api import sync_playwright

    scene = grouped_scene()
    file = tmp_path / "group.html"
    file.write_text(compile_scene(scene, width=1280, height=720, duration=4,
                                  style=StyleGuide()), encoding="utf-8")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ["SCID_CHROME"])
        try:
            page = browser.new_page(viewport={"width":1280,"height":720})
            page.goto(file.as_uri())
            def boxes(t: float):
                return page.evaluate("t=>{window.__seek(t); return ['person','prop'].map(id=>{const r=document.getElementById(id).getBoundingClientRect();return [r.x,r.width]})}", t)
            start = boxes(0)
            final = boxes(2)
            assert final[0][1] == pytest.approx(start[0][1]*1.1)
            assert (final[1][0]-final[0][0]) == pytest.approx((start[1][0]-start[0][0])*1.1)
            assert boxes(0) == start
            assert boxes(2) == final
        finally:
            browser.close()

    static = element("obstacle", x=.79, color="accent")
    static["box"]["width"] = .18
    scene.elements.append(SceneSpec.model_validate({"elements":[static]}).elements[0])
    file.write_text(compile_scene(scene, width=1280, height=720, duration=4,
                                  style=StyleGuide()), encoding="utf-8")
    report = inspect(file, 1280, 720, 4, 32, os.environ["SCID_CHROME"])
    assert any("同色图形重叠" in issue for issue in report["issues"]), report
