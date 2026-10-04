"""提示词模板的**结构性**守卫 —— 适用于 prompts 目录下的每一个模板。

这一组不是针对某个模板的文案检查，而是防一类**跨模板复发**的坑：

模板末尾常有一段 HTML 注释，用来说明"本模板有哪些变量"。如果那段注释里
把变量名写成占位符形式（双花括号），**渲染器会把它当成真的占位符替换掉** ——
注释于是变成一串被填进去的原始值，后果有两个：

  1. 大块内容（如 `previous_feedback`、`feedback_block`）被注入**两遍**，
     白烧一倍上下文；对审查来说更糟：上一轮的问题出现两次，
     等于把"重复上一轮结论"的锚定压力翻倍；
  2. 这些模板的注释都在**最末尾**，而提示词结尾对指令遵循影响最大 ——
     "只输出一个 JSON 对象"后面会跟上一串无意义数字。

`coder_user.md` 的 `feedback_block` 踩过一次（v0.6.18），
`critic_user.md` 的 `previous_feedback` 又踩了一次（v0.6.20）。
所以这里改成**扫描全部模板**，而不是逐个补测试。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scidirector_ai.config import get_settings

#: 匹配任意 HTML 注释块（模板里的"变量说明"用的就是它）。
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

#: 双花括号占位符。
_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def template_paths() -> list[Path]:
    prompts_dir = get_settings().resolved_prompts_dir
    return sorted(prompts_dir.glob("*.md"))


def test_there_are_templates_to_check() -> None:
    """守卫本身要有效：至少得扫到模板，否则这条测试会静默变成空转。"""
    paths = template_paths()
    assert len(paths) >= 5, f"只找到 {len(paths)} 个模板，路径可能不对"


@pytest.mark.parametrize("path", template_paths(), ids=lambda p: p.name)
def test_documentation_comment_has_no_placeholders(path: Path) -> None:
    """末尾的"变量说明"注释里不得出现占位符形式。

    要说明变量就**只写名字**（`previous_feedback`），不要写占位符
    （那样会被真的替换成值，见模块文档）。
    """
    text = path.read_text(encoding="utf-8")
    comments = _HTML_COMMENT.findall(text)
    offenders: list[str] = []
    for comment in comments:
        for name in _PLACEHOLDER.findall(comment):
            offenders.append(name)
    assert not offenders, (
        f"{path.name} 的注释里出现了占位符 {sorted(set(offenders))} —— "
        "渲染器会把它替换成真实值，导致内容被注入两遍、且提示词结尾变成噪声"
    )
