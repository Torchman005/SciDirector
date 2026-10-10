import importlib.util
import os
from pathlib import Path

import pytest

from scidirector_ai.agents.base import style_guide_to_text
from scidirector_ai.scene import SceneSpec, compile_scene
from scidirector_ai.schemas import StyleGuide


def demo() -> SceneSpec:
    location = Path(__file__).parents[2] / "scripts/render-anime-demo.py"
    spec = importlib.util.spec_from_file_location("anime_demo", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.demo_scene()


def test_direction_reaches_director_coder_and_critic_without_changing_old_jobs() -> None:
    assert "二次元科学动画" in style_guide_to_text(StyleGuide(animation_style="anime"))
    assert "二次元科学动画" not in style_guide_to_text(StyleGuide())


def test_demo_has_correct_reflection_geometry_and_no_random_animation() -> None:
    scene = demo()
    beams = [e for e in scene.elements if e.kind == "polyline"]
    assert beams[0].box.width == beams[1].box.width
    assert beams[0].box.height == beams[1].box.height
    assert beams[0].box.x + beams[0].box.width == beams[1].box.x
    code = compile_scene(scene,width=1280,height=720,duration=8,style=StyleGuide())
    assert "Math.random" not in code and "setInterval" not in code


def test_scale_cannot_clip_canvas_and_text_cannot_scale() -> None:
    scene = demo()
    scene.elements[-1].keyframes[1].scale = 1.5
    scene.elements[-1].box.x = .82
    with pytest.raises(ValueError,match="超出画布"):
        compile_scene(scene,width=1280,height=720,duration=8,style=StyleGuide())
    payload=demo().elements[0].model_dump()
    payload["keyframes"]=[{"time":0,"scale":.5}]
    with pytest.raises(ValueError):
        SceneSpec(elements=[payload])


@pytest.mark.skipif(not os.environ.get("SCID_CHROME"),reason="需要 Chromium")
def test_anime_real_browser_determinism_and_readability(tmp_path: Path) -> None:
    from scidirector_ai.browser_preflight import inspect
    page=tmp_path/"index.html"
    page.write_text(compile_scene(demo(),width=1280,height=720,duration=8,style=StyleGuide()),encoding="utf-8")
    report=inspect(page,1280,720,8,32,os.environ["SCID_CHROME"])
    assert not report["issues"],report
