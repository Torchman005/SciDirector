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
