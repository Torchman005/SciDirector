import pytest

manim = pytest.importorskip('manim')
from scidirector_ai.sandbox.manim_probe_runtime import _scid_install_layout_probe


def run_scene(build, area=None):
    class MeasuredScene(manim.Scene):
        def construct(self):
            self.add(build())
    _scid_install_layout_probe(MeasuredScene,area)
    # Exercise real Manim geometry without encoding an unnecessary video.
    scene=MeasuredScene()
    scene.construct()
    return scene


def test_text_outside_frame_and_presenter_overlap_are_actionable_errors():
    with pytest.raises(ValueError,match='SCID_LAYOUT.*文字超出画布.*actual.*label'):
        run_scene(lambda:manim.Text('actual label',font_size=40).shift(manim.RIGHT*7))
    with pytest.raises(ValueError,match='SCID_LAYOUT.*讲解员.*presenter.*conflict'):
        run_scene(lambda:manim.Text('presenter conflict',font_size=24).shift(manim.RIGHT*5+manim.DOWN*2),
                  {'x':.78,'y':.55,'width':.22,'height':.45})


def test_visible_nested_labels_checked_hidden_text_ignored_and_in_bounds_passes():
    run_scene(lambda:manim.Text('readable label',font_size=40))
    run_scene(lambda:manim.Text('hidden',font_size=40).shift(manim.RIGHT*20).set_opacity(0))
    with pytest.raises(ValueError,match='SCID_LAYOUT'):
        run_scene(lambda:manim.VGroup(manim.Text('nested').shift(manim.UP*5)))
