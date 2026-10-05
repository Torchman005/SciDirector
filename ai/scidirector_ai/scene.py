"""Versioned internal scene IR, compiled to the existing HTML code contract.

The model owns content and choreography; a trusted runtime owns DOM and seek.
No model supplied HTML, CSS, expressions or asset URLs are executed.
"""
from __future__ import annotations

import json
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


# Consistent with the generation quality gate: reserve 6% for content and 0.5s
# for reading the completed scene (10% for very short shots).
SAFE_MARGIN = .06
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
    easing: Literal["linear", "smooth", "step"] = "smooth"


class Datum(SceneModel):
    label: str = Field(min_length=1, max_length=80)
    value: float = Field(ge=0, le=1e12)


class Element(SceneModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,47}$")
    kind: Literal["text", "card", "code", "bars", "rect", "circle", "line"]
    box: Box
    text: str = Field(default="", max_length=4000)
    font_size: int = Field(default=48, ge=12, le=200)
    align: Literal["left", "center", "right"] = "left"
    color: str = Field(default="text", pattern=r"^(text|primary|muted|#[0-9a-fA-F]{6})$")
    data: list[Datum] = Field(default_factory=list, max_length=8)
    unit: str = Field(default="", max_length=30)
    keyframes: list[Keyframe] = Field(default_factory=list, max_length=24)
    arrow: bool = False

    @model_validator(mode="after")
    def content_and_time(self) -> Element:
        if self.kind in {"text", "card", "code"} and not self.text.strip():
            raise ValueError(f"#{self.id} 缺少文字内容")
        if self.kind == "bars" and (not self.data or not self.unit.strip()):
            raise ValueError(f"#{self.id} 柱状图必须提供真实数据及单位")
        if self.kind != "bars" and (self.data or self.unit):
            raise ValueError(f"#{self.id} 只有 bars 可以包含 data/unit")
        if self.kind in {"rect", "circle", "line"} and self.text:
            raise ValueError(f"#{self.id} 图形文字请使用独立 text 元素")
        if self.kind != "line" and self.arrow:
            raise ValueError(f"#{self.id} 只有 line 支持 arrow")
        if self.keyframes:
            times = [k.time for k in self.keyframes]
            if times[0] != 0 or any(b <= a for a, b in zip(times, times[1:])):
                raise ValueError(f"#{self.id} keyframes 必须从 0 开始且严格递增")
        if self.kind not in {"rect", "circle", "line"} and any(k.rotation for k in self.keyframes):
            raise ValueError(f"#{self.id} 文字/图表不可旋转；请用几何元素表达旋转")
        if self.kind not in {"code", "bars"} and any(k.reveal != 1 for k in self.keyframes):
            raise ValueError(f"#{self.id} reveal 仅支持 code/bars；其他元素请用 opacity")
        return self


class SceneSpec(SceneModel):
    version: Literal[1] = 1
    background: Literal["solid", "gradient", "grid", "vignette", "noise", "scanlines"] = "solid"
    elements: list[Element] = Field(min_length=1, max_length=48)
    explanation: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def unique_ids(self) -> SceneSpec:
        ids = [el.id for el in self.elements]
        if len(ids) != len(set(ids)):
            raise ValueError("场景元素 id 必须唯一")
        return self


def validate_layout(scene: SceneSpec, *, width: int, height: int, duration: float,
                    style: StyleGuide) -> None:
    """Reject geometric/time defects before launching Chromium; text fit is measured there."""
    hold = min(FINAL_HOLD_SEC, duration * FINAL_HOLD_RATIO)
    for el in scene.elements:
        text = el.kind in {"text", "card", "code", "bars"}
        if text and el.font_size < style.min_font_size:
            raise ValueError(f"#{el.id} 字号 {el.font_size}px 小于下限 {style.min_font_size}px")
        b = el.box
        for k in el.keyframes or [Keyframe(time=0)]:
            x, y = b.x + k.dx, b.y + k.dy
            if min(x, y) < SAFE_MARGIN - 1e-6 or x + b.width > 1 - SAFE_MARGIN + 1e-6 or y + b.height > 1 - SAFE_MARGIN + 1e-6:
                raise ValueError(f"#{el.id} 在 time={k.time:g} 超出 6% 安全边距；修改 box/dx/dy")
            if k.time > 1 - hold / duration + 1e-6:
                raise ValueError(f"#{el.id} 最后 {hold:g}s 应定格；提前完成 keyframes")
        # Rotating a non-square box sweeps a larger envelope between keyframes.
        if any(k.rotation for k in el.keyframes):
            radius = math.hypot(b.width * width, b.height * height) / 2
            for k in el.keyframes:
                cx, cy = b.x + b.width / 2 + k.dx, b.y + b.height / 2 + k.dy
                if cx - radius / width < SAFE_MARGIN or cx + radius / width > 1 - SAFE_MARGIN or cy - radius / height < SAFE_MARGIN or cy + radius / height > 1 - SAFE_MARGIN:
                    raise ValueError(f"#{el.id} 旋转范围超出安全边距；缩小或移动几何图形")
        if text:
            padding = el.font_size * .6 if el.kind in {"card", "code"} else 0
            if b.width * width < el.font_size * 2 + padding * 2 or b.height * height < el.font_size * 1.4 + padding * 2:
                raise ValueError(f"#{el.id} 文字区域过小；增大 box，不要降低字号")
        if el.kind == "bars" and b.height * height / len(el.data) < el.font_size * 3:
            raise ValueError(f"#{el.id} 柱状图行高不足；扩大区域或减少同屏数据")
    # Avoid an empty first/last frame; do not enforce cosmetic motion during reading holds.
    for final in (False, True):
        if not any(not el.keyframes or (el.keyframes[-1 if final else 0].opacity > .1
                                      and (el.kind not in {"code", "bars"}
                                           or el.keyframes[-1 if final else 0].reveal > 0))
                   for el in scene.elements):
            raise ValueError("首帧和末帧必须有可见内容")


@lru_cache(maxsize=1)
def scene_runtime() -> str:
    return Path(__file__).with_name("scene_runtime.js").read_text(encoding="utf-8")


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
    payload = {"scene": scene.model_dump(), "width": width, "height": height,
               "duration": duration, "style": {"background": style.background_color,
               "primary": style.primary_color, "font": style.font_family, "theme": style.theme,
               "backgroundCSS": background_css}}
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
