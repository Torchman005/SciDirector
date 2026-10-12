"""The paper theatre is a stable scene layer, independent of seek state."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from scidirector_ai.scene import SceneSpec, compile_scene
from scidirector_ai.schemas import StyleGuide


def paper_demo() -> SceneSpec:
    return SceneSpec.model_validate({"elements": [
        {"id": "headline", "kind": "text", "text": "把抽象概念放进故事里", "font_size": 44,
         "box": {"x": .08, "y": .08, "width": .82, "height": .14}},
        {"id": "idea", "kind": "card", "text": "一个清楚的科学问题", "font_size": 32,
         "color": "primary", "material": "outline", "box": {"x": .09, "y": .32, "width": .47, "height": .22}},
        {"id": "focus", "kind": "circle", "color": "accent", "material": "gradient",
         "box": {"x": .66, "y": .33, "width": .12, "height": .21},
         "keyframes": [{"time": 0, "opacity": 0, "dy": -.04}, {"time": .24, "opacity": 1}, {"time": 1, "opacity": 1}]},
    ], "explanation": "暖纸舞台上保留稳定地面，前景对象按节拍进入"})


def test_paper_stage_compiles_as_light_scene() -> None:
    style = StyleGuide(preset="paper", background_style="paper_stage", animation_style="anime")
    html = compile_scene(paper_demo(), width=960, height=540, duration=4, style=style)
    assert '"paperStage": true' in html
    assert 'paper-stage::after' in html
    assert '"theme": "light"' in html
    assert 'data:image/svg+xml' in html


@pytest.mark.skipif(not os.environ.get("SCID_CHROME"), reason="SCID_CHROME 未设置")
def test_real_browser_paper_stage_and_repeat_seek(tmp_path: Path) -> None:
    from playwright.sync_api import sync_playwright
    from scidirector_ai.browser_preflight import inspect

    page_file = tmp_path / "paper.html"
    page_file.write_text(compile_scene(paper_demo(), width=960, height=540, duration=4,
                                       style=StyleGuide(preset="paper", background_style="paper_stage", animation_style="anime")), encoding="utf-8")
    report = inspect(page_file, 960, 540, 4, 32, os.environ["SCID_CHROME"])
    assert not report["issues"], report
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ["SCID_CHROME"], args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 960, "height": 540})
        page.goto(page_file.as_uri())
        page.wait_for_function("window.__ready === true")
        assert page.locator("#stage").evaluate("e => e.classList.contains('paper-stage')")
        assert "polygon" in page.locator("#stage").evaluate("e => getComputedStyle(e,'::after').clipPath")
        page.evaluate("window.__seek(2)")
        before = page.screenshot(path=str(tmp_path / "paper-stage.png"))
        page.evaluate("window.__seek(4);window.__seek(0);window.__seek(2)")
        assert page.screenshot() == before
        browser.close()
