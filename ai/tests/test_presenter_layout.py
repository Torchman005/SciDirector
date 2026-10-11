import pytest

from scidirector_ai.scene import SceneSpec, compile_scene
from scidirector_ai.schemas import StyleGuide

AREA = dict(x=.78,y=.55,width=.22,height=.45)


def test_overlay_only_reserves_bottom_right_not_a_column():
    style = StyleGuide(presenter_safe_area=AREA)
    top = dict(id="top",kind="text",text="右侧上方仍能放动画",box=dict(x=.5,y=.1,width=.46,height=.2))
    assert compile_scene(SceneSpec(elements=[top]),width=1920,height=1080,duration=8,style=style)
    top["box"]["y"] = .6
    with pytest.raises(ValueError,match="讲解员"):
        compile_scene(SceneSpec(elements=[top]),width=1920,height=1080,duration=8,style=style)


def test_animated_object_cannot_cross_avatar_and_text_cannot_fake_decoration():
    shape=dict(id="token",kind="rect",box=dict(x=.5,y=.65,width=.12,height=.12),keyframes=[dict(time=0),dict(time=.8,dx=.3)])
    with pytest.raises(ValueError,match="讲解员"):
        compile_scene(SceneSpec(elements=[shape]),width=1920,height=1080,duration=8,style=StyleGuide(presenter_safe_area=AREA))
    with pytest.raises(ValueError,match="decorative"):
        SceneSpec(elements=[dict(id="label",kind="text",text="数据",decorative=True,box=dict(x=.8,y=.7,width=.18,height=.1))])
