"""多智能体实现。

阶段一提供：
    base.Agent              智能体基类与提示词装载
    director.DirectorAgent  导演智能体（脚本 -> 结构化分镜表）

阶段二补充：
    coder.CoderAgent        编码智能体（标签路由 -> Manim / D3 / 代码动画源码）
    critic.CriticAgent      审查智能体（VLM 抽帧审查 -> 可执行的修改建议）
"""

from .base import Agent, PromptNotFoundError, load_prompt, render_prompt, style_guide_to_text
from .director import DirectorAgent

__all__ = [
    "Agent",
    "DirectorAgent",
    "PromptNotFoundError",
    "load_prompt",
    "render_prompt",
    "style_guide_to_text",
]
