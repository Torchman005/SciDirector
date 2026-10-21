"""Deterministic layout normalization and content-semantic validation."""
from __future__ import annotations

from typing import Any

import pytest

from scidirector_ai.scene import (SceneSpec, compile_scene, extract_scene, normalize_layout,
                                  timeline_seconds_per_unit, validate_layout, _content_union,
                                  _sample_track)
from scidirector_ai.schemas import StyleGuide

W, H, D = 1920, 1080, 8
PRESENTER = dict(x=.78, y=.55, width=.22, height=.45)


def scene(*elements: dict[str, Any], groups: list[dict[str, Any]] | None = None) -> SceneSpec:
    return SceneSpec.model_validate({"elements": list(elements), "motion_groups": groups or []})


def box(x: float, y: float, w: float, h: float) -> dict[str, float]:
    return {"x": x, "y": y, "width": w, "height": h}


def crowded() -> SceneSpec:
    """A typical LLM layout: small objects squeezed into the top 40%."""
    return scene(
        {"id": "title", "kind": "text", "text": "前馈网络", "font_size": 40, "box": box(.06, .05, .3, .07)},
        {"id": "a", "kind": "card", "text": "输入", "font_size": 36, "box": box(.06, .16, .14, .1)},
        {"id": "b", "kind": "card", "text": "输出", "font_size": 36, "box": box(.3, .16, .14, .1)},
        {"id": "link", "kind": "line", "anchor_from": "a", "anchor_to": "b", "arrow": True},
        {"id": "note", "kind": "text", "text": "逐词元加工", "font_size": 32, "box": box(.06, .3, .3, .07)},
    )


def test_crowded_layout_is_enlarged_centered_and_still_valid() -> None:
    style = StyleGuide(presenter_safe_area=PRESENTER)
    original = crowded()
    result = normalize_layout(original, style, W, H, D)
    validate_layout(result, width=W, height=H, duration=D, style=style)
    before, after = _content_union(original, W, H), _content_union(result, W, H)
    k = (after[2] - after[0]) / (before[2] - before[0])
    assert 1.08 < k <= 1.6 + .01  # grid snapping moves edges by a few px
    fonts = {el.id: el.font_size for el in result.elements}
    assert fonts["a"] > 36 and fonts["title"] > 40 and max(fonts.values()) <= 96
    # Content now spans more of the frame vertically and stays above subtitles.
    assert after[3] - after[1] > before[3] - before[1]
    assert after[3] <= .85 + 1e-6
    # Anchored relations keep their full-stage placeholder.
    assert result.elements[3].box == original.elements[3].box


def test_presenter_area_limits_the_scale() -> None:
    style = StyleGuide(presenter_safe_area=PRESENTER)
    wide = scene({"id": "a", "kind": "card", "text": "宽幅主图", "font_size": 40, "box": box(.06, .1, .7, .3)},
                 {"id": "b", "kind": "card", "text": "下方注释", "font_size": 40, "box": box(.06, .5, .3, .12)})
    result = normalize_layout(wide, style, W, H, D)
    validate_layout(result, width=W, height=H, duration=D, style=style)
    a = result.elements[0].box
    assert a.x + a.width <= PRESENTER["x"] + 1e-6 or a.y + a.height <= PRESENTER["y"] + 1e-6


def test_boxes_snap_to_grid_and_siblings_share_size_and_rows() -> None:
    raw = scene(
        {"id": "a", "kind": "card", "text": "专家一", "font_size": 48, "box": box(.05, .1, .2, .2)},
        {"id": "b", "kind": "card", "text": "专家二", "font_size": 48, "box": box(.3, .105, .21, .19)},
        {"id": "c", "kind": "card", "text": "专家三", "font_size": 48, "box": box(.55, .1, .19, .205)},
        {"id": "caption", "kind": "text", "text": "八个专家并列", "font_size": 48, "box": box(.05, .4, .69, .45)},
    )
    result = normalize_layout(raw, StyleGuide(), W, H, D)
    cards = [el.box for el in result.elements[:3]]
    assert len({(round(b.width * W), round(b.height * H)) for b in cards}) == 1
    assert len({round((b.y + b.height / 2) * H) for b in cards}) == 1
    for el in result.elements:
        for value, total in ((el.box.x, W), (el.box.y, H), (el.box.width, W), (el.box.height, H)):
            assert abs(value * total / 8 - round(value * total / 8)) < 1e-6


def test_slow_fades_shortened_and_half_transparent_rest_snapped() -> None:
    raw = scene(
        {"id": "title", "kind": "text", "text": "标题", "font_size": 64, "box": box(.05, .05, .9, .14)},
        {"id": "slow", "kind": "card", "text": "慢淡入", "font_size": 48, "box": box(.05, .3, .4, .2),
         "keyframes": [{"time": .1, "opacity": 0}, {"time": .5}]},
        {"id": "ghost", "kind": "card", "text": "半透明", "font_size": 48, "box": box(.5, .3, .4, .2),
         "keyframes": [{"time": 0, "opacity": .5}]},
        {"id": "dim", "kind": "card", "text": "弱化", "font_size": 48, "box": box(.05, .6, .4, .2),
         "keyframes": [{"time": 0}, {"time": .5, "opacity": .2}]},
    )
    result = normalize_layout(raw, StyleGuide(), W, H, D)
    seconds = timeline_seconds_per_unit(result, D)
    slow = result.elements[1].keyframes
    assert slow[1].time == .5 and (slow[1].time - slow[0].time) * seconds <= .45 + 1e-9
    assert result.elements[2].keyframes[0].opacity == 1
    # A deliberate de-emphasis after full visibility stays, raised to readable.
    assert result.elements[3].keyframes[1].opacity == pytest.approx(.35)


def test_late_opening_is_pulled_forward() -> None:
    raw = scene(
        {"id": "a", "kind": "card", "text": "主物件", "font_size": 48, "box": box(.05, .1, .5, .3),
         "keyframes": [{"time": .3, "opacity": 0}, {"time": .35}, {"time": .8, "dx": .1}]},
        {"id": "b", "kind": "card", "text": "随后", "font_size": 48, "box": box(.05, .5, .5, .3),
         "keyframes": [{"time": .6, "opacity": 0}, {"time": .65}]},
    )
    result = normalize_layout(raw, StyleGuide(), W, H, D)
    seconds = timeline_seconds_per_unit(result, D)
    a, b = result.elements
    visible = next(t / 100 for t in range(101) if _sample_track(a.keyframes, t / 100 / seconds)[0] > .1)
    assert visible <= .5
    assert a.keyframes[-1].time == .8 and b.keyframes == raw.elements[1].keyframes


def test_invalid_scene_is_returned_unchanged_and_normalization_is_idempotent() -> None:
    invalid = scene({"id": "a", "kind": "card", "text": "太小", "font_size": 48, "box": box(.05, .05, .05, .05)})
    assert normalize_layout(invalid, StyleGuide(), W, H, D) is invalid
    once = normalize_layout(crowded(), StyleGuide(presenter_safe_area=PRESENTER), W, H, D)
    assert normalize_layout(once, StyleGuide(presenter_safe_area=PRESENTER), W, H, D) == once


def test_compile_persists_the_rendered_normalized_ir() -> None:
    style = StyleGuide(presenter_safe_area=PRESENTER)
    code = compile_scene(crowded(), width=W, height=H, duration=D, style=style)
    assert extract_scene(code) == normalize_layout(crowded(), style, W, H, D)
    assert "will-change:transform,opacity" in code


@pytest.mark.parametrize("extra,ok", [
    ({"id": "frame", "kind": "rect", "box": box(.5, .1, .4, .3)}, False),
    ({"id": "frame", "kind": "circle", "box": box(.5, .1, .3, .3)}, False),
    ({"id": "frame", "kind": "rect", "box": box(.5, .1, .1, .1)}, True),  # small accent
    ({"id": "frame", "kind": "rect", "decorative": True, "box": box(.5, .1, .4, .3)}, True),
    ({"id": "frame", "kind": "rect", "overlap_group": "x", "box": box(.5, .1, .4, .3)}, True),
])
def test_empty_placeholder_shapes_are_rejected(extra: dict[str, Any], ok: bool) -> None:
    other = {"id": "label", "kind": "card", "text": "标签", "font_size": 40, "box": box(.05, .6, .3, .15)}
    if extra.get("overlap_group"):
        other["overlap_group"] = "x"
    s = scene(extra, other)
    if ok:
        validate_layout(s, width=W, height=H, duration=D, style=StyleGuide())
    else:
        with pytest.raises(ValueError, match="card"):
            validate_layout(s, width=W, height=H, duration=D, style=StyleGuide())


def test_labelled_shape_and_connector_endpoint_are_content() -> None:
    labelled = scene({"id": "frame", "kind": "rect", "box": box(.5, .1, .4, .3)},
                     {"id": "label", "kind": "text", "text": "容器", "font_size": 40, "box": box(.55, .15, .2, .1)})
    validate_layout(labelled, width=W, height=H, duration=D, style=StyleGuide())
    endpoint = scene({"id": "a", "kind": "circle", "box": box(.05, .1, .3, .3)},
                     {"id": "b", "kind": "card", "text": "结果", "font_size": 40, "box": box(.6, .1, .3, .2)},
                     {"id": "l", "kind": "line", "anchor_from": "a", "anchor_to": "b"})
    validate_layout(endpoint, width=W, height=H, duration=D, style=StyleGuide())


@pytest.mark.parametrize("text,ok", [("~", False), ("…", False), ("-", False), ("?", True), ("⊗", True), ("x", True)])
def test_symbol_only_text_is_rejected(text: str, ok: bool) -> None:
    s = scene({"id": "t", "kind": "text", "text": text, "font_size": 48, "box": box(.1, .1, .2, .1)})
    if ok:
        validate_layout(s, width=W, height=H, duration=D, style=StyleGuide())
    else:
        with pytest.raises(ValueError, match="标点"):
            validate_layout(s, width=W, height=H, duration=D, style=StyleGuide())


def test_free_line_touching_a_moving_object_must_be_anchored() -> None:
    # 来自真实成片：token 沿 dx 移动，而"轨道"是一条自由折线，结果线悬在原地。
    from scidirector_ai.scene import SceneSpec, validate_layout
    from scidirector_ai.schemas import StyleGuide
    scene = SceneSpec.model_validate({"elements": [
        {"id": "expert", "kind": "card", "text": "专家 6", "font_size": 44,
         "box": {"x": .24, "y": .3, "width": .2, "height": .12}},
        {"id": "token", "kind": "card", "text": "token", "font_size": 44,
         "box": {"x": .06, "y": .62, "width": .16, "height": .14},
         "keyframes": [{"time": .3, "opacity": 0}, {"time": .45}, {"time": .9, "dx": .36}]},
        {"id": "track", "kind": "polyline", "box": {"x": .14, "y": .42, "width": .2, "height": .14},
         "points": [{"x": 0, "y": 1}, {"x": .5, "y": 1}, {"x": .5, "y": 0}]},
    ]})
    with pytest.raises(ValueError, match="anchor_from"):
        validate_layout(scene, width=1920, height=1080, duration=6, style=StyleGuide())
