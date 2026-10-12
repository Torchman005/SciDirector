"""Imported stills must survive style checkpoints and render as decoded pixels."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from PIL import Image

from scidirector_ai.pbconv import style_guide_from_json
from scidirector_ai.scene import SceneSpec, compile_scene
from scidirector_ai.schemas import StyleGuide


ASSET_ID = "a" * 32
THINKING_ID = "c" * 32


def imported_style(tmp_path: Path, *, opaque: bool = False) -> StyleGuide:
    directory = tmp_path / "visuals" / ("b" * 64)
    directory.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    for y in range(100):
        for x in range(100 if opaque else 20):
            image.putpixel((x, y), (230, 80, 95, 255))
    path = directory / f"{ASSET_ID}.png"
    image.save(path)
    payload = {
        "min_font_size": 12,
        "scene_assets": [{"asset_id": ASSET_ID, "filename": "speaker.png", "mime_type": "image/png",
                          "role": "character", "description": "讲解员", "subject": "小葵", "pose": "待机",
                          "width": 100, "height": 100,
                          "size_bytes": path.stat().st_size}],
        "scene_asset_sources": {ASSET_ID: str(path)},
    }
    style = style_guide_from_json(json.dumps(payload))
    assert style.scene_asset_sources[ASSET_ID] == str(path)
    assert StyleGuide.model_validate(style.model_dump()).scene_asset_sources[ASSET_ID] == str(path)
    return style


def variant_style(tmp_path: Path) -> StyleGuide:
    style = imported_style(tmp_path)
    source = Path(style.scene_asset_sources[ASSET_ID])
    variant = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    for y in range(100):
        for x in range(70, 90):
            variant.putpixel((x, y), (70, 110, 220, 255))
    target = source.with_name(f"{THINKING_ID}.png")
    variant.save(target)
    style.scene_assets.append(type(style.scene_assets[0]).model_validate({
        "asset_id": THINKING_ID, "filename": "thinking.png", "mime_type": "image/png",
        "role": "character", "description": "讲解员思考", "subject": "小葵", "pose": "思考",
        "width": 100, "height": 100, "size_bytes": target.stat().st_size,
    }))
    style.scene_asset_sources[THINKING_ID] = str(target)
    return style


def switching_scene() -> SceneSpec:
    return SceneSpec.model_validate({"elements": [
        {"id": "portrait", "kind": "image", "asset_id": ASSET_ID,
         "box": {"x": .15, "y": .12, "width": .6, "height": .76},
         "keyframes": [{"time": 0}, {"time": .5, "asset_id": THINKING_ID}]},
    ]})


def image_scene() -> SceneSpec:
    return SceneSpec.model_validate({"elements": [
        {"id": "portrait", "kind": "image", "asset_id": ASSET_ID,
         "box": {"x": .05, "y": .2, "width": .55, "height": .6},
         "keyframes": [{"time": 0, "opacity": 1}, {"time": .5, "dx": .1, "opacity": 1}]},
        {"id": "caption", "kind": "text", "text": "安全区", "font_size": 16,
         "box": {"x": .4, "y": .4, "width": .2, "height": .2}},
    ], "explanation": "透明立绘和标题"})


def test_selected_image_is_embedded_and_unselected_image_is_rejected(tmp_path: Path) -> None:
    style = imported_style(tmp_path)
    html = compile_scene(image_scene(), width=320, height=180, duration=2, style=style)
    assert f'data:image/png;base64,' in html
    assert '"asset_id": "' + ASSET_ID + '"' in html
    style = StyleGuide.model_validate({**style.model_dump(), "presenter_safe_area": {"x": .5, "y": 0, "width": .5, "height": 1}})
    image_only = SceneSpec.model_validate({"elements": [image_scene().elements[0].model_dump()]})
    # A transparent image box may cross the presenter region; only decoded
    # painted pixels decide whether it actually obscures the presenter.
    compile_scene(image_only, width=320, height=180, duration=2, style=style)
    with pytest.raises(ValueError, match="未选中"):
        compile_scene(image_scene(), width=320, height=180, duration=2, style=StyleGuide())
    style.presenter_safe_area = None
    style.scene_asset_sources.clear()
    with pytest.raises(ValueError, match="缺少服务端解析路径"):
        compile_scene(image_scene(), width=320, height=180, duration=2, style=style)


def test_pose_switch_requires_selected_matching_subject_role_and_canvas(tmp_path: Path) -> None:
    scene = switching_scene()
    style = variant_style(tmp_path)
    html = compile_scene(scene, width=320, height=180, duration=2, style=style)
    assert f'"{THINKING_ID}": "data:image/png;base64,' in html
    for field, value in (("subject", "other"), ("role", "prop"), ("width", 99)):
        bad = style.model_copy(deep=True)
        setattr(bad.scene_assets[1], field, value)
        with pytest.raises(ValueError, match="同一对象"):
            compile_scene(scene, width=320, height=180, duration=2, style=bad)
    bad = style.model_copy(deep=True)
    bad.scene_assets.pop()
    with pytest.raises(ValueError, match="未选中"):
        compile_scene(scene, width=320, height=180, duration=2, style=bad)
    with pytest.raises(ValueError, match="仅用于 image"):
        SceneSpec.model_validate({"elements": [{"id": "title", "kind": "text", "text": "x",
            "box": {"x": .1, "y": .1, "width": .4, "height": .2},
            "keyframes": [{"time": 0, "asset_id": THINKING_ID}]}]})


@pytest.mark.skipif(not os.environ.get("SCID_CHROME"), reason="SCID_CHROME 未设置")
def test_image_alpha_occlusion_decode_and_reverse_seek(tmp_path: Path) -> None:
    from scidirector_ai.browser_preflight import inspect

    page = tmp_path / "scene.html"
    html = compile_scene(image_scene(), width=320, height=180, duration=2, style=imported_style(tmp_path))
    page.write_text(html, encoding="utf-8")
    good = inspect(page, 320, 180, 2, 12, os.environ["SCID_CHROME"])
    assert not good["issues"], good

    full = compile_scene(image_scene(), width=320, height=180, duration=2, style=imported_style(tmp_path, opaque=True))
    page.write_text(full, encoding="utf-8")
    blocked = inspect(page, 320, 180, 2, 12, os.environ["SCID_CHROME"])
    assert any("遮挡" in issue for issue in blocked["issues"]), blocked

    page.write_text(html.replace("data:image/png;base64,", "data:image/png;base64,broken"), encoding="utf-8")
    broken = inspect(page, 320, 180, 2, 12, os.environ["SCID_CHROME"])
    assert any("解码失败" in issue for issue in broken["issues"]), broken


@pytest.mark.skipif(not os.environ.get("SCID_CHROME"), reason="SCID_CHROME 未设置")
def test_pose_switch_crossfade_and_reverse_seek(tmp_path: Path) -> None:
    from playwright.sync_api import sync_playwright
    from scidirector_ai.browser_preflight import inspect, sample_times

    style = variant_style(tmp_path)
    page_path = tmp_path / "pose.html"
    page_path.write_text(compile_scene(switching_scene(), width=320, height=180,
                                       duration=2, style=style), encoding="utf-8")
    payload = json.loads(page_path.read_text(encoding="utf-8").split('<script id="scid-scene-v1" type="application/json">')[1].split('</script>')[0])
    assert any(abs(t - 1.09) < .001 for t in sample_times(2, payload))
    report = inspect(page_path, 320, 180, 2, 12, os.environ["SCID_CHROME"])
    assert not report["issues"], report
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ["SCID_CHROME"], args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport={"width": 320, "height": 180})
            page.goto(page_path.resolve().as_uri())
            page.wait_for_function("() => window.__ready")
            def state(at: float) -> dict[str, float]:
                return page.evaluate("t => {window.__seek(t); return Object.fromEntries([...document.querySelectorAll('#portrait img')].map(i=>[i.dataset.assetId, Number(i.style.opacity)]))}", at)
            before = state(.8)
            middle = state(1.09)
            after = state(1.4)
            assert before[ASSET_ID] == 1 and before[THINKING_ID] == 0
            assert .4 < middle[ASSET_ID] < .6 and .4 < middle[THINKING_ID] < .6
            assert after[ASSET_ID] == 0 and after[THINKING_ID] == 1
            assert state(.8) == before
            assert state(1.09) == middle
        finally:
            browser.close()
