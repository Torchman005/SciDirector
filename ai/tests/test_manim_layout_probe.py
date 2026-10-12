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


def test_overlapping_independent_labels_are_rejected_but_separated_labels_pass():
    with pytest.raises(ValueError,match='SCID_LAYOUT.*独立文字重叠'):
        run_scene(lambda:manim.VGroup(manim.Text('ReLU'),manim.Text('SwiGLU').shift(manim.RIGHT*.1)))
    run_scene(lambda:manim.VGroup(manim.Text('ReLU'),manim.Text('SwiGLU')).arrange(manim.DOWN,buff=.2))


def test_place_preserves_group_geometry_fonts_and_avoids_presenter():
    import numpy as np
    area = {'x':.78,'y':.55,'width':.22,'height':.45}
    class PlacedScene(manim.Scene):
        def construct(self):
            label = manim.Text('Measured label',font_size=32)
            graph = manim.Rectangle(width=4,height=2).next_to(label,manim.UP)
            group = manim.VGroup(label,graph)
            delta = graph.get_center()-label.get_center()
            size = (group.width,group.height,label.width,label.font_size)
            assert self.scid_place(group,(.45,.30,.50,.60)) is group
            assert np.allclose(delta,graph.get_center()-label.get_center())
            assert np.allclose(size,(group.width,group.height,label.width,label.font_size))
            right = group.get_right()[0]/self.camera.frame_width+.5
            top = .5-group.get_top()[1]/self.camera.frame_height
            bottom = .5-group.get_bottom()[1]/self.camera.frame_height
            assert right < area['x'] or bottom < area['y']
            assert top >= .30 and right <= .95
            self.add(group)
    _scid_install_layout_probe(PlacedScene,area)
    PlacedScene().construct()


def test_place_rejects_oversize_group_without_moving_or_shrinking_it():
    import numpy as np
    class PlacedScene(manim.Scene):
        def construct(self):pass
    _scid_install_layout_probe(PlacedScene,None)
    scene = PlacedScene()
    obj = manim.Text('Must keep readable size',font_size=48)
    before = (obj.width,obj.height,*obj.get_center())
    with pytest.raises(ValueError,match='SCID_LAYOUT.*图组放不下'):
        scene.scid_place(obj,(.05,.05,.15,.15))
    assert np.allclose(before,(obj.width,obj.height,*obj.get_center()))
    for region in [(0,0,1.1,1),(float('nan'),0,1,1),(0,0,0,1)]:
        with pytest.raises(ValueError,match='SCID_LAYOUT'):
            scene.scid_place(obj,region)


def test_place_uses_current_moving_camera_and_full_width_upper_region():
    import numpy as np
    class PlacedScene(manim.MovingCameraScene):
        def construct(self):pass
    _scid_install_layout_probe(PlacedScene,{'x':.78,'y':.55,'width':.22,'height':.45})
    scene=PlacedScene()
    scene.camera.frame.shift(manim.RIGHT*2).scale(.8)
    obj=manim.Text('Wide title',font_size=32)
    scene.scid_place(obj,(.04,.04,.92,.18))
    frame=scene.camera.frame
    assert np.allclose(obj.get_center()[:2],
        frame.get_center()[:2]+[0,(.5-.13)*frame.height])
