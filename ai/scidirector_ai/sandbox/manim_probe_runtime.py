"""Trusted probe appended AFTER generated source has passed the safety policy.

Runs inside the rendering subprocess. Measures text at stable animation/hold
endpoints and construct completion, never during Write/FadeIn transitions.
"""


def _scid_install_layout_probe(scene_class, presenter_area):
    from manim import Text, MarkupText, Tex, MathTex, DecimalNumber
    import numpy as np

    text_types = (Text, MarkupText, Tex, MathTex, DecimalNumber)

    def camera_rect(scene):
        camera = scene.camera
        frame = getattr(camera, 'frame', None)
        if frame is not None:
            cx, cy = frame.get_center()[:2]
            width, height = frame.width, frame.height
        else:
            cx, cy = camera.frame_center[:2]
            width, height = camera.frame_width, camera.frame_height
        return cx, cy, width, height

    def place(scene, group, region=(.04, .04, .92, .92)):
        """Translate an explicit semantic group into a normalized safe region.

        Never scales fonts, alters math, or moves a label independently. The
        caller owns grouping, connections and stage visibility. Layout that
        cannot fit is rejected, not silently degraded.
        """
        cx, cy, width, height = camera_rect(scene)
        if len(region) != 4 or not all(np.isfinite(v) for v in region):
            raise ValueError('SCID_LAYOUT scid_place region 必须是四个有限数值')
        x, y, w, h = region
        if width <= 0 or height <= 0 or x < 0 or y < 0 or w <= 0 or h <= 0 or x+w > 1.000001 or y+h > 1.000001:
            raise ValueError('SCID_LAYOUT scid_place region 必须位于归一化画布内')
        gw, gh = group.width / width, group.height / height
        if not np.isfinite(gw + gh) or gw <= 0 or gh <= 0:
            raise ValueError('SCID_LAYOUT scid_place 需要有有限正尺寸的图组')
        # A small inset separates content from the exact exclusion boundary.
        gap = .006
        regions = [(x, y, w, h)]
        if presenter_area:
            p = presenter_area
            # All maximal rectangles around the exclusion, intersected with the
            # requested region; handles arbitrary presenter positions as well.
            regions = [
                (x, y, min(x+w, p['x']-gap)-x, h),
                (max(x, p['x']+p['width']+gap), y, x+w-max(x, p['x']+p['width']+gap), h),
                (x, y, w, min(y+h, p['y']-gap)-y),
                (x, max(y, p['y']+p['height']+gap), w, y+h-max(y, p['y']+p['height']+gap)),
            ]
        candidates = []
        desired = (x+w/2, y+h/2)
        for rx, ry, rw, rh in regions:
            if gw > rw + 1e-6 or gh > rh + 1e-6:
                continue
            px = min(max(desired[0], rx+gw/2), rx+rw-gw/2)
            py = min(max(desired[1], ry+gh/2), ry+rh-gh/2)
            candidates.append(((px-desired[0])**2 + (py-desired[1])**2, px, py))
        if not candidates:
            raise ValueError(f'SCID_LAYOUT 图组放不下：实际归一化尺寸 {gw:.3f}×{gh:.3f}，区域 {region}；'
                             '请拆分展示阶段、换行或缩短坐标轴，保持字号与科学条件，不缩放整图')
        _, px, py = min(candidates)
        group.move_to([cx+(px-.5)*width, cy+(.5-py)*height, group.get_center()[2]])
        return group

    def check(scene):
        cx, cy, width, height = camera_rect(scene)
        if width <= 0 or height <= 0:
            return
        seen = set()
        text_rects = []
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
            for old_label, a0, b0, a1, b1 in text_rects:
                overlap = max(0, min(x1,a1)-max(x0,a0)) * max(0, min(y1,b1)-max(y0,b0))
                smaller = min((x1-x0)*(y1-y0), (a1-a0)*(b1-b0))
                # Ignore touching edges/rounding. Text objects are atomic:
                # MathTex's internal glyphs are never tested against each other.
                if overlap > smaller * .08:
                    raise ValueError(f'SCID_LAYOUT 独立文字重叠：{old_label!r} 与 {label!r}；'
                                     f'当前边界 {rect}；time={scene.time:.2f}s；'
                                     '请分配不相交区域或分阶段展示，不能把叠放的两组一起平移')
            text_rects.append((label,x0,y0,x1,y1))
        for obj in scene.mobjects:
            visit(obj)

    original_play = scene_class.play
    original_wait = scene_class.wait
    original_construct = scene_class.construct

    def play(scene, *args, **kwargs):
        result = original_play(scene, *args, **kwargs)
        check(scene)
        return result

    def construct(scene):
        result = original_construct(scene)
        check(scene)
        return result

    def wait(scene, *args, **kwargs):
        result = original_wait(scene, *args, **kwargs)
        check(scene)
        return result

    scene_class.play = play
    scene_class.wait = wait
    scene_class.construct = construct
    scene_class.scid_place = place
