"""智能体基类与提示词装载。

为什么把提示词放在独立的 ``.md`` 文件而不是 Python 字符串里？
* **可版本化与可评审**：改一句提示词应当像改代码一样可 diff、可回滚；
* **可灰度 / A/B**：不同版本可以并存，按流量分配；
* **避免转义地狱**：提示词里大量 JSON 示例与反引号，写在 Python 字符串里极易出错。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from ..config import get_settings
from ..llm import LLMClient
from ..logging import get_logger
from ..schemas import StyleGuide

logger = get_logger(__name__)


class PromptNotFoundError(FileNotFoundError):
    """提示词文件缺失。这属于部署问题，应当快速失败而不是静默降级。"""


@lru_cache(maxsize=64)
def load_prompt(name: str) -> str:
    """按名称装载提示词模板（带缓存）。

    文件名约定：``<name>.md``，位于 ``agents/prompts/`` 下。
    模板中使用 ``{{variable}}`` 占位，由 ``render_prompt`` 做**简单替换**。

    刻意不使用 Jinja2：这里的模板不需要循环/条件（复杂逻辑应当写在 Python 里），
    引入模板引擎只会让提示词的调试链路变长。
    """
    path = get_settings().resolved_prompts_dir / f"{name}.md"
    if not path.is_file():
        raise PromptNotFoundError(f"提示词文件不存在：{path}")
    return path.read_text(encoding="utf-8")


def render_prompt(name: str, **variables: Any) -> str:
    """装载并做占位替换。

    未提供的占位符保持原样（而不是替换成空串），这样漏传变量时能一眼看出来。
    """
    template = load_prompt(name)
    for key, value in variables.items():
        template = template.replace("{{" + key + "}}", _stringify(value))
    return template


def _stringify(value: Any) -> str:
    """把变量渲染为提示词友好的字符串。"""
    if value is None:
        return "（未提供）"
    if isinstance(value, str):
        return value
    if isinstance(value, StyleGuide):
        return style_guide_to_text(value)
    return str(value)


def style_guide_to_text(guide: StyleGuide) -> str:
    """把风格约束渲染成模型易读的要点列表。

    这一步很有价值：直接把 Pydantic 的 repr 塞进提示词，模型对
    ``primary_color='#4F8CFF'`` 这种写法的利用效率远低于自然语言要点。
    """
    lines = [
        f"- 主题模式：{'深色' if guide.theme == 'dark' else '浅色'}",
        f"- 背景色：{guide.background_color}",
        f"- 主色调：{guide.primary_color}",
        f"- 字体：{guide.font_family}",
        f"- 正文字号不小于：{guide.min_font_size}px（低于此值会被审查判为不可读）",
        f"- 画面比例：{guide.aspect_ratio}（{guide.resolution[0]}x{guide.resolution[1]}）",
    ]
    if guide.glossary:
        pairs = "；".join(f"{k} -> {v}" for k, v in guide.glossary.items())
        lines.append(f"- 术语表（必须严格遵守，保证全片译名一致）：{pairs}")
    return "\n".join(lines)


class Agent:
    """智能体基类。

    所有智能体都必须满足（见 Agent.md §5）：
    * 输入输出为**可校验的结构化对象**；
    * **幂等**：同样输入产生同样输出（温度尽量低）；
    * 单次调用内**不产生外部副作用**（唯一的例外是沙盒渲染，且它不属于智能体职责）。
    """

    #: 子类覆盖：提示词文件名（不含 .md）
    prompt_name: str = ""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or LLMClient(get_settings())
        self.log = get_logger(f"{__name__}.{type(self).__name__}")

    @property
    def name(self) -> str:
        return type(self).__name__

    def system_prompt(self) -> str:
        """返回本智能体的系统提示词。子类可覆盖以注入动态内容。"""
        if not self.prompt_name:
            raise NotImplementedError(f"{self.name} 未声明 prompt_name")
        return load_prompt(self.prompt_name)
