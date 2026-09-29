"""背景样式与风格预设的契约测试。

本文件的作用是**钉住两侧的表**：背景样式 id 与风格预设配色在 Go 与 Python
各有一份（Go 那份用于效果预览，Python 这份用于生成），必须逐项一致。

这与本项目处理引擎映射的惯例相同：两边各写一份（而不是跨语言读文件 ——
CI 里未必有 Python 环境）+ 一条测试**指名对侧**，单边修改立刻变红。

分叉的后果很具体：预览用 A 配色、成片用 B 配色，用户会照着预览去调参数。
"""

from __future__ import annotations

import re

import pytest

from scidirector_ai.backgrounds import (
    BACKGROUND_STYLE_IDS,
    BACKGROUND_STYLES,
    background_prompt_block,
    resolve_background_style,
)
from scidirector_ai.schemas import STYLE_PRESETS, StyleGuide

#: Go 侧 media/background.go 的 backgroundStyleIDs。
#: 改动这里必须同时改那边（对侧由 backend/internal/media/preview_test.go 钉住）。
GO_BACKGROUND_STYLE_IDS = (
    "auto",
    "solid",
    "gradient",
    "grid",
    "vignette",
    "noise",
    "scanlines",
)

#: Go 侧 media/background.go 的 stylePresets。
GO_STYLE_PRESETS = {
    "default": ("#4F8CFF", "#0B1020"),
    "tech": ("#3DDC97", "#08111F"),
    "warm": ("#FF8A4C", "#1A1013"),
    "minimal": ("#E6ECFF", "#101216"),
    "nature": ("#5FD68A", "#0B1A12"),
    "sunset": ("#FF6B9D", "#1B1020"),
}


def test_background_style_ids_match_go_side() -> None:
    assert BACKGROUND_STYLE_IDS == GO_BACKGROUND_STYLE_IDS, (
        "背景样式 id 与 Go 侧不一致；预览会画不出某个样式，或前端能选但服务端报错"
    )


def test_style_presets_match_go_side() -> None:
    assert set(STYLE_PRESETS) == set(GO_STYLE_PRESETS), "风格预设 id 两侧不一致"
    for name, (primary, background) in GO_STYLE_PRESETS.items():
        palette = STYLE_PRESETS[name]
        assert palette["primary_color"] == primary, f"预设 {name} 的主色两侧不一致"
        assert palette["background_color"] == background, f"预设 {name} 的背景色两侧不一致"


def test_every_style_is_fully_described() -> None:
    for style_id, style in BACKGROUND_STYLES.items():
        assert style.id == style_id
        assert style.label, f"{style_id} 缺中文名（前端要显示它）"
        assert style.prompt, f"{style_id} 缺提示词描述"
        assert style.css, f"{style_id} 缺 CSS 片段"
        assert style.manim, f"{style_id} 缺 manim 做法说明"


def test_css_uses_no_remote_assets() -> None:
    """CSS 里不允许引用远程资源。

    内联 SVG 的 `xmlns` 是命名空间标识符、不会发起请求，因此只约束 `url(...)`：
    凡是用到 url()，必须是 data: URI。沙盒无网络，任何 CDN 引用都会让渲染
    静默降级成一片空白（而不是报错），排查起来极其费劲。
    """
    for style_id, style in BACKGROUND_STYLES.items():
        for url in re.findall(r"url\(([^)]*)\)", style.css):
            assert url.strip().strip("\"'").startswith("data:"), (
                f"{style_id} 的 CSS 引用了非内联资源：{url[:60]}"
            )


def test_css_actually_substitutes_colours() -> None:
    guide = StyleGuide(preset="tech", background_style="grid")
    block = background_prompt_block(guide)
    # 代入后不该再留下未替换的占位符 —— 那会让模型照着 "{primary}" 去画。
    assert "{bg}" not in block
    assert "{primary}" not in block
    assert guide.background_color in block
    assert guide.primary_color in block


def test_non_auto_style_demands_varied_composition() -> None:
    """非 auto 的样式必须要求「样式统一、构图不同」。

    只要求"用这个背景样式"而不要求变化，会退化成用户此前抱怨的
    「只有文字变化，背景不变化」—— 那正是这次要修的问题。
    """
    for style_id in BACKGROUND_STYLE_IDS:
        if style_id == "auto":
            continue
        block = background_prompt_block(StyleGuide(background_style=style_id))
        assert "构图必须不同" in block, f"{style_id} 没有要求逐镜头变化背景构图"


def test_auto_style_defers_to_the_model() -> None:
    block = background_prompt_block(StyleGuide(background_style="auto"))
    assert "每个镜头的背景构图必须不同" in block


def test_unknown_style_is_rejected() -> None:
    with pytest.raises(ValueError) as exc:
        resolve_background_style("plaid")
    assert "未知的背景样式" in str(exc.value)
    assert "grid" in str(exc.value), "错误信息应当列出可用值"

    with pytest.raises(Exception):
        StyleGuide(background_style="plaid")


def test_default_style_is_auto() -> None:
    # 缺省必须是 auto：不填任何东西时行为与既有任务完全一致。
    assert StyleGuide().background_style == "auto"
