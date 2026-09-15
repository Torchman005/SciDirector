"""多智能体实现。

    base.Agent              智能体基类与提示词装载
    director.DirectorAgent  导演智能体（脚本 -> 结构化分镜表，含时长预算修复）
    coder.CoderAgent        编码智能体（按标签路由生成 Manim / HTML / 代码动画源码）
    critic.CriticAgent      审查智能体（VLM 抽帧审查 -> 可执行的修改建议）

四个智能体的共同契约（见 Agent.md §5）：
    * 输入输出为**可校验的结构化对象**；
    * **幂等**：同样输入产生同样输出（审查温度固定为 0）；
    * 单次调用内**不产生外部副作用**（唯一的例外是渲染，且它不属于智能体职责）。
"""

from .base import Agent, PromptNotFoundError, load_prompt, render_prompt, style_guide_to_text
from .coder import (
    PROMPT_BY_ENGINE,
    CodeArtifact,
    CodeGenerationResult,
    CoderAgent,
    load_engine_prompt,
    render_examples,
    title_from,
)
from .critic import CriticAgent, CritiqueOutcome
from .director import DirectorAgent

__all__ = [
    "PROMPT_BY_ENGINE",
    "Agent",
    "CodeArtifact",
    "CodeGenerationResult",
    "CoderAgent",
    "CriticAgent",
    "CritiqueOutcome",
    "DirectorAgent",
    "PromptNotFoundError",
    "load_engine_prompt",
    "load_prompt",
    "render_examples",
    "render_prompt",
    "style_guide_to_text",
    "title_from",
]
