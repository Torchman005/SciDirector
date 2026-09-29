"""背景样式预设。

**这个表是"背景长什么样"的唯一真源**：提示词与 CSS 片段都由它生成。
Go 侧（`media/background.go`）有一份**同 id** 的表，用来在效果预览里把背景
渲染出来 —— 两边的 id 列表由一条 Python 契约测试钉住（与本项目处理引擎映射
的做法一致：两边各写一份 + 一条测试指名对侧）。

为什么预设是"风格族"而不是"一张背景图"：
用户此前反馈过「只有文字变化，背景不变化」。若把全片每帧背景锁死成同一个样式，
那正是把当时的问题又固化一遍。因此每个预设只规定**风格与配色**，
并要求模型在每个镜头里做出变化（网格疏密、光晕位置、颗粒强度…）。

`auto` 是缺省值，表示"由模型按内容决定"—— 保持与既有任务完全一致的行为。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅用于类型标注
    # 运行时**不导入** schemas：schemas 的校验器需要导入本模块，
    # 两边在模块层互相导入会直接变成循环导入。标注上开了
    # `from __future__ import annotations`，因此这里的名字只是字符串。
    from .schemas import StyleGuide

#: 背景样式的 id 列表（顺序即前端下拉框顺序）。
#: Go 侧 media/background.go 必须提供同一组 id，由 test_backgrounds.py 钉住。
BACKGROUND_STYLE_IDS: tuple[str, ...] = (
    "auto",
    "solid",
    "gradient",
    "grid",
    "vignette",
    "noise",
    "scanlines",
)


@dataclass(frozen=True)
class BackgroundStyle:
    """一个背景样式的全部描述。"""

    #: 对外 id（也是配置里写的值）。
    id: str
    #: 中文名，用于提示词与前端展示。
    label: str
    #: 给模型的一句风格描述。
    prompt: str
    #: HTML 引擎可直接使用的 CSS（已代入真实配色，不含 CDN）。
    #: `{bg}` / `{primary}` 由 css_for() 代入。
    css: str
    #: manim 的建议做法。manim 没有 CSS，只能用它自己的图元近似。
    manim: str


#: 内置背景样式。
_STYLES: dict[str, BackgroundStyle] = {
    "auto": BackgroundStyle(
        id="auto",
        label="自动（按内容决定）",
        prompt="由你根据内容自行决定背景，但**每个镜头的背景构图必须不同**。",
        css="background-color:{bg};",
        manim="自行决定，但每个镜头要让背景构图不同。",
    ),
    "solid": BackgroundStyle(
        id="solid",
        label="纯色",
        prompt="干净的纯色背景，靠前景元素的层次拉开空间感。",
        css="background-color:{bg};",
        manim="self.camera.background_color = 背景色即可。",
    ),
    "gradient": BackgroundStyle(
        id="gradient",
        label="渐变",
        prompt="从背景色向主色过渡的斜向渐变，营造纵深。",
        css=(
            "background: linear-gradient(135deg, {bg} 0%, {bg} 55%, {primary}33 100%);"
        ),
        manim=(
            "manim 没有原生渐变背景：用**同一色系的纯色背景**，"
            "再叠一个主色的柔和圆形光晕（多个同心圆、透明度递减）近似即可。"
        ),
    ),
    "grid": BackgroundStyle(
        id="grid",
        label="坐标纸网格",
        prompt="细网格背景，像坐标纸，给画面一个可度量的空间。",
        css=(
            "background-color:{bg};"
            "background-image:"
            "linear-gradient({primary}1f 1px, transparent 1px),"
            "linear-gradient(90deg, {primary}1f 1px, transparent 1px);"
            "background-size: 64px 64px;"
        ),
        manim="用 NumberPlane 或 VGroup 画细直线网格，颜色取主色的低透明度版本。",
    ),
    "vignette": BackgroundStyle(
        id="vignette",
        label="暗角聚光",
        prompt="中心亮、四周压暗的聚光背景，把视线收拢到画面中部。",
        css=(
            "background: radial-gradient(ellipse at center,"
            " {primary}1a 0%, {bg} 55%, #000000 100%);"
        ),
        manim="用若干层透明度递减的同心圆叠出中心亮、四周暗的效果。",
    ),
    "noise": BackgroundStyle(
        id="noise",
        label="细颗粒",
        prompt="带极细颗粒质感的深色背景，避免大面积平色显得廉价。",
        css=(
            "background-color:{bg};"
            "background-image:url(\"data:image/svg+xml,"
            "%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='120'%3E"
            "%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise'"
            " baseFrequency='0.85' numOctaves='3'/%3E%3C/filter%3E"
            "%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.06'/%3E"
            "%3C/svg%3E\");"
        ),
        manim="manim 没有噪点：改为纯色背景 + 前景元素的细微抖动，不要硬凑噪点。",
    ),
    "scanlines": BackgroundStyle(
        id="scanlines",
        label="扫描线",
        prompt="等距细横线背景，带一点显示器/示波器的味道。",
        css=(
            "background-color:{bg};"
            "background-image: repeating-linear-gradient(0deg,"
            " {primary}14 0px, {primary}14 1px, transparent 1px, transparent 5px);"
        ),
        manim="用一组等距的水平细线（Line 或 VGroup）铺满背景。",
    ),
}

#: 对外的只读视图，按 BACKGROUND_STYLE_IDS 的顺序。
BACKGROUND_STYLES: dict[str, BackgroundStyle] = {k: _STYLES[k] for k in BACKGROUND_STYLE_IDS}

DEFAULT_BACKGROUND_STYLE = "auto"


def resolve_background_style(style_id: str | None) -> BackgroundStyle:
    """按 id 取背景样式；未登记时**报错**，不静默回落。

    静默回落的表现是"用户选了网格、成片却是纯色"，而且没有任何地方提示过 ——
    与本项目在调色方案、风格预设上采取的态度一致。
    """
    key = (style_id or "").strip().lower() or DEFAULT_BACKGROUND_STYLE
    if key not in BACKGROUND_STYLES:
        raise ValueError(
            f"未知的背景样式 {style_id!r}；可用：{', '.join(BACKGROUND_STYLE_IDS)}"
        )
    return BACKGROUND_STYLES[key]


def background_prompt_block(guide: StyleGuide) -> str:
    """生成注入提示词用的背景说明块（含可直接使用的 CSS）。"""
    style = resolve_background_style(getattr(guide, "background_style", None))
    css = style.css.format(bg=guide.background_color, primary=guide.primary_color)
    lines = [
        f"- 背景样式：**{style.label}** —— {style.prompt}",
        f"  - HTML 引擎请直接采用这段 CSS（配色已代入，可微调尺寸/角度）：`{css}`",
        f"  - manim：{style.manim}",
    ]
    # "auto" 之外都要求**样式统一、构图不同**：统一才像一部片子，
    # 构图不同才不会退化成"同一张背景在换文字"（用户此前正是这么反馈的）。
    if style.id != "auto":
        lines.append(
            "  - **同一部片子里所有镜头都用这个背景样式，但每个镜头的背景构图必须不同**"
            "（网格疏密/偏移、光晕位置与强度、颗粒浓度、线条间距…）。"
            "背景完全雷同会显得片子很廉价。"
        )
    return "\n".join(lines)
