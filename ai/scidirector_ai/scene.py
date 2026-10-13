"""Versioned internal scene IR, compiled to the existing HTML code contract.

The model owns content and choreography; a trusted runtime owns DOM and seek.
No model supplied HTML, CSS, expressions or asset URLs are executed.
"""
from __future__ import annotations

import json
import base64
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .backgrounds import resolve_background_style
from .schemas import StyleGuide


class SceneModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


# Tolerate decimal roundoff only; visible geometry must remain on the canvas.
GEOMETRY_EPSILON = 1e-4
COLOR_PATTERN = r"^(text|primary|secondary|accent|success|warning|muted|#[0-9a-fA-F]{6})$"
# The compiler reserves 0.5s for the final state (10% for very short shots).
FINAL_HOLD_SEC = .5
FINAL_HOLD_RATIO = .1


class Box(SceneModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def bounds(self) -> Box:
        if self.x + self.width > 1.000001 or self.y + self.height > 1.000001:
            raise ValueError("box 超出画布")
        return self


class Keyframe(SceneModel):
    time: float = Field(ge=0, le=1, description="镜头时间 / duration_sec")
    opacity: float = Field(default=1, ge=0, le=1)
    dx: float = Field(default=0, ge=-1, le=1)
    dy: float = Field(default=0, ge=-1, le=1)
    rotation: float = Field(default=0, ge=-3600, le=3600)
    reveal: float = Field(default=1, ge=0, le=1)
    scale: float = Field(default=1, ge=.5, le=1.5)
    expression: Literal["neutral", "smile", "curious", "surprised", "focused"] = "neutral"
    gesture: Literal["idle", "explain", "point", "think", "wave"] = "idle"
    easing: Literal["linear", "smooth", "step", "ease_in", "ease_out", "spring"] = "smooth"
    asset_id: str = Field(default="", pattern=r"^$|^[0-9a-f]{32}$")


class GroupKeyframe(SceneModel):
    time: float = Field(ge=0, le=1)
    opacity: float = Field(default=1, ge=0, le=1)
    dx: float = Field(default=0, ge=-.25, le=.25)
    dy: float = Field(default=0, ge=-.25, le=.25)
    scale: float = Field(default=1, ge=.85, le=1.15)
    easing: Literal["linear", "smooth", "step", "ease_in", "ease_out", "spring"] = "smooth"


class MotionGroup(SceneModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,47}$")
    keyframes: list[GroupKeyframe] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def ordered(self) -> MotionGroup:
        times = [frame.time for frame in self.keyframes]
        if any(b <= a for a, b in zip(times, times[1:])):
            raise ValueError(f"运动组 #{self.id} 的 keyframes 必须严格递增")
        return self


class Point(SceneModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class Datum(SceneModel):
    label: str = Field(min_length=1, max_length=80)
    value: float = Field(ge=0, le=1e12)
    color: str = Field(default="", pattern=r"^$|" + COLOR_PATTERN)


class GridCell(SceneModel):
    label: str = Field(default="", max_length=24)
    color: str = Field(default="", pattern=r"^$|" + COLOR_PATTERN)


class Element(SceneModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,47}$")
    kind: Literal["text", "card", "code", "bars", "rect", "circle", "line", "polyline", "character", "icon", "grid", "image"]
    box: Box
    text: str = Field(default="", max_length=4000)
    font_size: int = Field(default=48, ge=12, le=200)
    align: Literal["left", "center", "right"] = "left"
    color: str = Field(default="text", pattern=COLOR_PATTERN)
    material: Literal["auto", "solid", "gradient", "glass", "outline"] = "auto"
    layer: int = Field(default=0, ge=-10, le=20)
    corner_radius: float = Field(default=16, ge=0, le=80)
    stroke_color: str = Field(default="", pattern=r"^$|" + COLOR_PATTERN)
    data: list[Datum] = Field(default_factory=list, max_length=8)
    unit: str = Field(default="", max_length=30)
    keyframes: list[Keyframe] = Field(default_factory=list, max_length=24)
    arrow: bool = False
    anchor_from: str = Field(default="", pattern=r"^$|^[A-Za-z][A-Za-z0-9_-]{0,47}$")
    anchor_to: str = Field(default="", pattern=r"^$|^[A-Za-z][A-Za-z0-9_-]{0,47}$")
    points: list[Point] = Field(default_factory=list, max_length=64)
    stroke_width: float = Field(default=4, ge=1, le=20)
    overlap_group: str = Field(default="", pattern=r"^[A-Za-z0-9_-]{0,48}$")
    decorative: bool = False
    glyph: Literal["chip", "memory", "router", "factory", "gate", "lens"] = "chip"
    cells: list[GridCell] = Field(default_factory=list, max_length=64)
    columns: int = Field(default=4, ge=1, le=8)
    flow: bool = False
    asset_id: str = Field(default="", pattern=r"^$|^[0-9a-f]{32}$")
    image_fit: Literal["contain", "cover"] = "contain"
    motion_group: str = Field(default="", pattern=r"^[A-Za-z0-9_-]{0,48}$")

    @model_validator(mode="before")
    @classmethod
    def explicit_line_path(cls, value: object) -> object:
        # Both represent the same path primitive; do not spend an LLM retry on
        # a line that already supplies valid explicit points.
        if isinstance(value, dict) and (value.get("anchor_from") or value.get("anchor_to")):
            value = dict(value)
            value.setdefault("box", {"x": 0, "y": 0, "width": 1, "height": 1})
        if isinstance(value, dict) and value.get("kind") in {"line", "polyline"} and isinstance(value.get("box"), dict) and not (value.get("anchor_from") or value.get("anchor_to")):
            b = value["box"]
            if b.get("width") == 0 and b.get("height") == 0:
                raise ValueError("关系线必须有非零长度")
            if b.get("width") == 0 or b.get("height") == 0:
                value = dict(value)
                b = dict(b)
                points = [dict(p) for p in value.get("points", [])] or [dict(x=0,y=0),dict(x=1,y=1)]
                for dimension, axis in (("width","x"),("height","y")):
                    if b.get(dimension) == 0:
                        b[dimension] = .006
                        b[axis] = max(0, min(.994, b[axis]-.003))
                        for p in points: p[axis] = .5
                value.update(box=b, kind="polyline", points=points)
        if isinstance(value, dict) and value.get("kind") == "line" and value.get("points"):
            return {**value, "kind": "polyline"}
        return value

    @model_validator(mode="after")
    def content_and_time(self) -> Element:
        if self.anchor_from or self.anchor_to:
            if self.kind != "line" or not self.anchor_from or not self.anchor_to:
                raise ValueError(f"#{self.id} 锚定关系必须是 line 且同时提供 anchor_from/anchor_to")
            if self.points or self.motion_group or self.decorative:
                raise ValueError(f"#{self.id} 锚定关系线不能使用 points、motion_group 或 decorative")
            if any(k.dx or k.dy or k.rotation or k.scale != 1 for k in self.keyframes):
                raise ValueError(f"#{self.id} 锚定关系线只支持 opacity/reveal，不要手动移动或缩放")
        if self.kind == "image" and not self.asset_id:
            raise ValueError(f"#{self.id} 图片必须引用已选择的 asset_id")
        if self.kind != "image" and self.asset_id:
            raise ValueError(f"#{self.id} asset_id 仅用于 image")
        if self.kind != "image" and any(frame.asset_id for frame in self.keyframes):
            raise ValueError(f"#{self.id} 关键帧 asset_id 仅用于 image")
        if self.kind == "grid" and not self.cells:
            raise ValueError(f"#{self.id} grid 必须提供 cells 标签；示意网格不编造数值")
        if self.kind != "grid" and self.cells:
            raise ValueError(f"#{self.id} cells 仅用于 grid")
        if self.flow and self.kind not in {"line", "polyline"}:
            raise ValueError(f"#{self.id} flow 仅用于有明确方向的关系线")
        if self.decorative and self.kind not in {"rect", "circle", "line", "polyline"}:
            raise ValueError("只有无语义的背景几何可以标记 decorative")
        if self.kind in {"text", "card", "code"} and not self.text.strip():
            raise ValueError(f"#{self.id} 缺少文字内容")
        if self.kind == "bars" and (not self.data or not self.unit.strip()):
            raise ValueError(f"#{self.id} 柱状图必须提供真实数据及单位")
        if self.kind != "bars" and (self.data or self.unit):
            raise ValueError(f"#{self.id} 只有 bars 可以包含 data/unit")
        if self.kind in {"rect", "circle", "line", "polyline", "character", "icon", "grid", "image"} and self.text:
            raise ValueError(f"#{self.id} 图形文字请使用独立 text 元素")
        if self.kind not in {"line", "polyline"} and self.arrow:
            raise ValueError(f"#{self.id} 只有 line 支持 arrow")
        if self.keyframes:
            times = [k.time for k in self.keyframes]
            if any(b <= a for a, b in zip(times, times[1:])):
                raise ValueError(f"#{self.id} keyframes 必须严格递增")
        if self.kind == "polyline" and len(self.points) < 2:
            raise ValueError(f"#{self.id} polyline 至少需要两个 points")
        if self.kind != "polyline" and self.points:
            raise ValueError(f"#{self.id} points 仅用于 polyline")
        if self.kind not in {"rect", "circle", "line", "polyline", "character", "icon", "image"} and any(k.rotation for k in self.keyframes):
            raise ValueError(f"#{self.id} 文字/图表不可旋转；请用几何元素表达旋转")
        if self.kind not in {"code", "bars", "line", "polyline", "grid"} and any(k.reveal != 1 for k in self.keyframes):
            raise ValueError(f"#{self.id} reveal 仅支持 code/bars/line/polyline；其他元素请用 opacity")
        if self.kind != "character" and any(k.expression != "neutral" or k.gesture != "idle" for k in self.keyframes):
            raise ValueError(f"#{self.id} expression/gesture 仅用于 character")
        return self


class SceneSpec(SceneModel):
    version: Literal[1] = 1
    background: Literal["solid", "gradient", "grid", "vignette", "noise", "scanlines", "paper_stage"] = "solid"
    elements: list[Element] = Field(min_length=1, max_length=48)
    motion_groups: list[MotionGroup] = Field(default_factory=list, max_length=8)
    explanation: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def unique_ids(self) -> SceneSpec:
        ids = [el.id for el in self.elements]
        if len(ids) != len(set(ids)):
            raise ValueError("场景元素 id 必须唯一")
        groups = [group.id for group in self.motion_groups]
        if len(groups) != len(set(groups)):
            raise ValueError("运动组 id 必须唯一")
        by_id = {element.id: element for element in self.elements}
        for element in self.elements:
            if element.motion_group and element.motion_group not in groups:
                raise ValueError(f"#{element.id} 引用了不存在的运动组 {element.motion_group}")
            if element.anchor_from:
                if element.anchor_from == element.anchor_to or element.id in {element.anchor_from, element.anchor_to}:
                    raise ValueError(f"#{element.id} 关系线两端必须是不同对象")
                for target_id in (element.anchor_from, element.anchor_to):
                    target = by_id.get(target_id)
                    if target is None or target.decorative or target.kind in {"line", "polyline"}:
                        raise ValueError(f"#{element.id} 锚点 {target_id} 必须引用存在的非装饰对象")
        for group in self.motion_groups:
            if sum(element.motion_group == group.id for element in self.elements) < 2:
                raise ValueError(f"运动组 #{group.id} 至少需要两个元素")
        return self


def validate_layout(scene: SceneSpec, *, width: int, height: int, duration: float,
                    style: StyleGuide) -> None:
    """Reject geometric/time defects before launching Chromium; text fit is measured there."""
    allowed_assets = {asset.asset_id: asset for asset in style.scene_assets}
    for el in scene.elements:
        if el.anchor_from:
            # Anchored lines use the full-stage SVG at runtime; the placeholder
            # box is not their painted geometry. Chromium checks the real route.
            continue
        if el.kind == "image":
            ids = [el.asset_id, *(frame.asset_id for frame in el.keyframes if frame.asset_id)]
            if any(asset_id not in allowed_assets for asset_id in ids):
                raise ValueError(f"#{el.id} 引用了未选中的图片素材；只能使用本任务 scene_assets 的 asset_id")
            first = allowed_assets[el.asset_id]
            for asset_id in ids[1:]:
                other = allowed_assets[asset_id]
                if (not first.subject or first.subject != other.subject or first.role != other.role or
                        (first.width, first.height) != (other.width, other.height)):
                    raise ValueError(f"#{el.id} 状态切换必须使用同一对象、同一用途和相同画布尺寸的图片")
        text = el.kind in {"text", "card", "code", "bars", "grid"}
        if text and el.font_size < style.min_font_size:
            raise ValueError(f"#{el.id} 字号 {el.font_size}px 小于下限 {style.min_font_size}px")
        b = el.box
        for k in el.keyframes or [Keyframe(time=0)]:
            if text and k.opacity > .9 and el.font_size * k.scale < style.min_font_size * .9:
                raise ValueError(f"#{el.id} 缩放后的实际字号 {el.font_size * k.scale:g}px 过小；提高字号或 scale")
            x, y = b.x + k.dx - b.width * (k.scale - 1) / 2, b.y + k.dy - b.height * (k.scale - 1) / 2
            a = style.presenter_safe_area
            # Image boxes can contain large transparent margins. The browser
            # checks decoded painted pixels against this same presenter area.
            if a and el.kind != "image" and not el.decorative and k.opacity > .1 and x < a.x+a.width and x+b.width*k.scale > a.x and y < a.y+a.height and y+b.height*k.scale > a.y:
                raise ValueError(f"#{el.id} 在 time={k.time:g} 进入右下讲解员区域；移动实际内容，只局部避让，不要缩小整幅画面")
            # An entrance can begin outside the viewport while fully transparent.
            # Safe margins are a design recommendation, actual clipping is a defect.
            if k.opacity > .1 and (min(x, y) < -GEOMETRY_EPSILON or x + b.width * k.scale > 1 + GEOMETRY_EPSILON or y + b.height * k.scale > 1 + GEOMETRY_EPSILON):
                raise ValueError(f"#{el.id} 在 time={k.time:g} 超出画布；修改 box/dx/dy")
        # Rotating a non-square box sweeps a larger envelope between keyframes.
        if any(k.rotation for k in el.keyframes):
            radius = math.hypot(b.width * width, b.height * height) / 2 * max(k.scale for k in el.keyframes)
            for k in el.keyframes:
                cx, cy = b.x + b.width / 2 + k.dx, b.y + b.height / 2 + k.dy
                if k.opacity > .1 and (cx - radius / width < -GEOMETRY_EPSILON or cx + radius / width > 1 + GEOMETRY_EPSILON or cy - radius / height < -GEOMETRY_EPSILON or cy + radius / height > 1 + GEOMETRY_EPSILON):
                    raise ValueError(f"#{el.id} 旋转范围超出画布；缩小或移动几何图形")
        if text:
            padding = el.font_size * .6 if el.kind in {"card", "code"} else 0
            if b.width * width < el.font_size * 2 + padding * 2 or b.height * height < el.font_size * 1.4 + padding * 2:
                raise ValueError(f"#{el.id} 文字区域过小；增大 box，不要降低字号")
        if el.kind == "bars" and b.height * height / len(el.data) < el.font_size * 3:
            raise ValueError(f"#{el.id} 柱状图行高不足；扩大区域或减少同屏数据")
        if el.kind == "grid":
            cols = min(el.columns, len(el.cells))
            rows = math.ceil(len(el.cells)/cols)
            if b.height*height/rows < el.font_size*1.8 or b.width*width/cols < el.font_size*2.4:
                raise ValueError(f"#{el.id} 网格格子过小；减少列/行或扩大 box")
    def sample_track(frames: list[Keyframe] | list[GroupKeyframe], at: float) -> tuple[float, float, float, float]:
        if not frames:
            return 1, 0, 0, 1
        first = frames[0]
        if at < first.time:
            return 0, first.dx, first.dy, first.scale
        previous = first
        for current in frames[1:]:
            if at < current.time:
                fraction = (at - previous.time) / (current.time - previous.time)
                if current.easing == "step": fraction = 0
                elif current.easing == "smooth": fraction = fraction*fraction*(3-2*fraction)
                elif current.easing == "ease_in": fraction = fraction**3
                elif current.easing == "ease_out": fraction = 1-(1-fraction)**3
                elif current.easing == "spring": fraction = (1-(1+7*fraction)*math.exp(-7*fraction))/(1-8*math.exp(-7))
                return tuple(getattr(previous, key)+(getattr(current, key)-getattr(previous, key))*fraction
                             for key in ("opacity", "dx", "dy", "scale"))
            previous = current
        return previous.opacity, previous.dx, previous.dy, previous.scale

    for group in scene.motion_groups:
        members = [el for el in scene.elements if el.motion_group == group.id]
        left = min(el.box.x for el in members)
        top = min(el.box.y for el in members)
        right = max(el.box.x + el.box.width for el in members)
        bottom = max(el.box.y + el.box.height for el in members)
        pivot_x, pivot_y = (left+right)/2, (top+bottom)/2
        key_times = sorted({0., 1., *(frame.time for frame in group.keyframes),
                            *(frame.time for el in members for frame in el.keyframes)})
        probe_times = sorted(set(key_times) | {(a+b)/2 for a, b in zip(key_times, key_times[1:])})
        for at in probe_times:
            group_opacity, group_dx, group_dy, group_scale = sample_track(group.keyframes, at)
            for el in members:
                opacity, dx, dy, scale = sample_track(el.keyframes, at)
                if group_opacity*opacity <= .1:
                    continue
                box = el.box
                cx, cy = box.x+box.width/2, box.y+box.height/2
                actual_cx = pivot_x + group_scale*(cx+dx-pivot_x) + group_dx
                actual_cy = pivot_y + group_scale*(cy+dy-pivot_y) + group_dy
                actual_scale = group_scale*scale
                x, y = actual_cx-box.width*actual_scale/2, actual_cy-box.height*actual_scale/2
                right_edge, bottom_edge = x+box.width*actual_scale, y+box.height*actual_scale
                if min(x, y) < -GEOMETRY_EPSILON or right_edge > 1+GEOMETRY_EPSILON or bottom_edge > 1+GEOMETRY_EPSILON:
                    raise ValueError(f"运动组 #{group.id} 使 #{el.id} 在 time={at:g} 超出画布")
                safe = style.presenter_safe_area
                if safe and el.kind != "image" and not el.decorative and x < safe.x+safe.width and right_edge > safe.x and y < safe.y+safe.height and bottom_edge > safe.y:
                    raise ValueError(f"运动组 #{group.id} 使 #{el.id} 在 time={at:g} 进入右下讲解员区域")
                if el.kind in {"text", "card", "code", "bars", "grid"} and el.font_size*actual_scale < style.min_font_size*.9:
                    raise ValueError(f"运动组 #{group.id} 使 #{el.id} 的实际字号过小")
    # The first frame may intentionally establish a scene. The final state must
    # contain visible content; otherwise the renderer would produce a blank tail.
    groups_by_id = {group.id: group for group in scene.motion_groups}
    if not any((not el.keyframes or (el.keyframes[-1].opacity > .1
                                     and (el.kind not in {"code", "bars", "grid"}
                                          or el.keyframes[-1].reveal > 0)))
               and (not el.motion_group or sample_track(groups_by_id[el.motion_group].keyframes, 1)[0] > .1)
               for el in scene.elements):
        raise ValueError("末帧必须有可见内容")


@lru_cache(maxsize=1)
def scene_runtime() -> str:
    return (Path(__file__).with_name("character_runtime.js").read_text(encoding="utf-8") + "\n"
            + Path(__file__).with_name("science_icons_runtime.js").read_text(encoding="utf-8") + "\n"
            + Path(__file__).with_name("scene_runtime.js").read_text(encoding="utf-8"))


def compile_scene(scene: SceneSpec, *, width: int, height: int, duration: float,
                  style: StyleGuide) -> str:
    validate_layout(scene, width=width, height=height, duration=duration, style=style)
    for value in (style.background_color, style.primary_color):
        if not re.fullmatch(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?", value):
            raise ValueError("结构化场景风格颜色必须为 #RGB 或 #RRGGBB")
    # Reuse the authoritative presets. Expand short colors before alpha suffixes.
    def full_hex(value: str) -> str:
        return "#" + "".join(c * 2 for c in value[1:]) if len(value) == 4 else value
    background = resolve_background_style(scene.background if style.background_style == "auto" else style.background_style)
    background_css = background.css.format(bg=full_hex(style.background_color), primary=full_hex(style.primary_color))
    image_data: dict[str, str] = {}
    allowed = {asset.asset_id: asset for asset in style.scene_assets}
    image_ids = {el.asset_id for el in scene.elements if el.kind == "image"}
    image_ids.update(frame.asset_id for el in scene.elements if el.kind == "image"
                     for frame in el.keyframes if frame.asset_id)
    for asset_id in sorted(image_ids):
        asset = allowed[asset_id]
        source = style.scene_asset_sources.get(asset_id)
        if not source:
            raise ValueError(f"图片素材 {asset_id} 缺少服务端解析路径")
        path = Path(source).resolve()
        suffix = ".png" if asset.mime_type == "image/png" else ".jpg"
        if (path.stem != asset_id or path.suffix.lower() != suffix or
                path.parent.parent.name != "visuals" or not re.fullmatch(r"[0-9a-f]{64}", path.parent.name)):
            raise ValueError(f"图片素材 {asset_id} 的路径无效")
        if not path.is_file() or path.stat().st_size != asset.size_bytes or asset.size_bytes > 2 << 20:
            raise ValueError(f"图片素材 {asset_id} 已丢失或内容被改动")
        image_data[asset_id] = f"data:{asset.mime_type};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
    # Reserve the final reading hold in the compiler rather than forcing the LLM
    # to calculate a duration-specific fraction. An end keyframe at 1 is legal.
    animated_until = 1 - min(FINAL_HOLD_SEC, duration * FINAL_HOLD_RATIO) / duration
    payload = {"scene": scene.model_dump(), "imageData": image_data,
               "imageRoles": {asset_id: allowed[asset_id].role for asset_id in image_ids},
               "width": width, "height": height,
               "animatedUntil": animated_until,
               "duration": duration, "style": {"background": style.background_color,
               "primary": style.primary_color, "font": style.font_family, "theme": style.theme,
               "animationStyle": style.animation_style,
               "presenterSafeArea": style.presenter_safe_area.model_dump() if style.presenter_safe_area else None,
               "backgroundCSS": background_css, "paperStage": background.id == "paper_stage"}}
    # JSON in a raw-text script must not be able to terminate its containing tag.
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c").replace("&", "\\u0026")
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<style>html,body{margin:0;width:100%;height:100%;overflow:hidden}"
            "*{box-sizing:border-box}#stage{position:absolute;inset:0;overflow:hidden}"
            ".scene-element{position:absolute;transform-origin:center;white-space:pre-wrap;"
            "overflow-wrap:anywhere;line-height:1.4}"
            ".scene-text{display:flex;flex-direction:column;justify-content:center}"
            ".scene-card,.scene-code{border-radius:18px;border:2px solid;padding:.6em}"
            ".scene-code{font-family:Consolas,'Noto Sans Mono CJK SC',monospace;tab-size:2}"
            ".scene-bar-row{display:flex;flex-direction:column;justify-content:center;gap:.15em}"
            ".scene-bar-label{display:flex;justify-content:space-between;gap:1em}"
            ".scene-bar-fill{height:.45em;border-radius:.2em;transform-origin:left}"
            "#stage.paper-stage{isolation:isolate}"
            "#stage.paper-stage::before{content:'';position:absolute;left:0;right:0;top:72%;height:1px;"
            "background:linear-gradient(90deg,transparent,#B6A38A80 22%,#B6A38A80 78%,transparent);z-index:-19}"
            "#stage.paper-stage::after{content:'';position:absolute;left:-1%;top:72%;width:102%;height:29%;"
            "background:repeating-linear-gradient(90deg,#8B75640a 0px,#8B75640a 2px,transparent 2px,transparent 30px),"
            "linear-gradient(180deg,#C9AD8C26,#D9C4A73d);"
            "clip-path:polygon(31% 0,69% 0,100% 100%,0 100%);z-index:-18;pointer-events:none}"
            "#stage.paper-stage img.scene-image{filter:drop-shadow(0 12px 9px #6B4B3B38)}"
            "svg{display:block;width:100%;height:100%;overflow:visible}"
            "</style></head><body><div id=\"stage\"></div>"
            f'<script id="scid-scene-v1" type="application/json">{data}</script>'
            f"<script>{scene_runtime()}</script></body></html>")


def extract_scene(code: str) -> SceneSpec | None:
    """Read persisted IR for revisions, never send the generated runtime back to the model."""
    match = re.search(r'<script id="scid-scene-v1" type="application/json">(.*?)</script>', code, re.S)
    if not match:
        return None
    payload = json.loads(match.group(1))
    if not isinstance(payload, dict) or "scene" not in payload:
        raise ValueError("HTML 中的场景记录缺少 scene 对象")
    return SceneSpec.model_validate(payload["scene"])
