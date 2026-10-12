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
                          "role": "character", "description": "讲解员", "width": 100, "height": 100,
                          "size_bytes": path.stat().st_size}],
        "scene_asset_sources": {ASSET_ID: str(path)},
    }
    style = style_guide_from_json(json.dumps(payload))
    assert style.scene_asset_sources[ASSET_ID] == str(path)
    assert StyleGuide.model_validate(style.model_dump()).scene_asset_sources[ASSET_ID] == str(path)
    return style


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
