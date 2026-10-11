"""Trusted probe appended AFTER generated source has passed the safety policy.

Runs inside the rendering subprocess. Measures text at stable animation endpoints
and construct completion, never during legitimate Write/FadeIn transitions.
"""


def _scid_install_layout_probe(scene_class, presenter_area):
    from manim import Text, MarkupText, Tex, MathTex, DecimalNumber
    import numpy as np

    text_types = (Text, MarkupText, Tex, MathTex, DecimalNumber)

    def check(scene):
        camera = scene.camera
        frame = getattr(camera, 'frame', None)
        if frame is not None:
            cx, cy = frame.get_center()[:2]
            width, height = frame.width, frame.height
        else:
            cx, cy = camera.frame_center[:2]
            width, height = camera.frame_width, camera.frame_height
        if width <= 0 or height <= 0:
            return
        seen = set()
        def visit(obj):
            if id(obj) in seen:
                return
            seen.add(id(obj))
            if not isinstance(obj, text_types):
                for child in obj.submobjects:
                    visit(child)
                return
            visible = any(
                float(np.max(m.get_fill_opacity())) > .05 or
                (float(np.max(m.get_stroke_opacity())) > .05 and float(np.max(m.get_stroke_width())) > 0)
                for m in obj.family_members_with_points())
            if not visible or obj.width <= 0 or obj.height <= 0:
                return
            x0 = (obj.get_left()[0] - cx) / width + .5
            x1 = (obj.get_right()[0] - cx) / width + .5
            y0 = .5 - (obj.get_top()[1] - cy) / height
            y1 = .5 - (obj.get_bottom()[1] - cy) / height
            label = str(getattr(obj, 'text', '') or getattr(obj, 'tex_string', '') or type(obj).__name__)[:90]
            rect = f'x={x0:.3f}～{x1:.3f}, y={y0:.3f}～{y1:.3f}'
            reason = ''
            if x0 < -.002 or y0 < -.002 or x1 > 1.002 or y1 > 1.002:
                reason = '文字超出画布，请换行或重新布局，保持可读字号'
            elif presenter_area:
                p = presenter_area
                if min(x1, p['x']+p['width']) > max(x0, p['x']) and min(y1, p['y']+p['height']) > max(y0, p['y']):
                    reason = '文字进入右下讲解员区域，请移到该矩形上方或左方，不缩整幅画面'
            if reason:
                raise ValueError(f'SCID_LAYOUT {reason}；文字={label!r}；实际边界 {rect}；time={scene.time:.2f}s')
        for obj in scene.mobjects:
            visit(obj)

    original_play = scene_class.play
    original_construct = scene_class.construct

    def play(scene, *args, **kwargs):
        result = original_play(scene, *args, **kwargs)
        check(scene)
        return result

    def construct(scene):
        result = original_construct(scene)
        check(scene)
        return result

    scene_class.play = play
    scene_class.construct = construct
