"""注意力机制章节：章节模式的端到端原型与后续生成的参考范例。

数值均为示意（d=64），用于演示 softmax 缩放的效果，不是实测模型输出。
"""
from __future__ import annotations

from pathlib import Path

from ..narration import ChapterLine

SERIES = "LLM 架构与优化 · 03"
TITLE = "注意力机制"
HIGHLIGHTS = {"Query": "#E8735A", "Key": "#2C9F95", "Value": "#6B62D6",
              "softmax": "#3A6FD8", "注意力机制": "#3A6FD8", "上下文": "#3A6FD8"}

LINES = [
    ChapterLine("hello", "哈喽，今天来拆解大模型里最核心的零件——注意力机制。"),
    ChapterLine("sent", "先看一句话：小猫没有喝牛奶，因为它太烫了。"),
    ChapterLine("who", "这里的“它”，指的是谁？"),
    ChapterLine("milk", "你一下就知道，是牛奶。"),
    ChapterLine("hungry", "可如果把“烫”换成“饿”，“它”就变成了小猫。"),
    ChapterLine("context", "同一个字，意思完全取决于上下文。注意力机制，就是让模型学会看上下文的方法。"),
    ChapterLine("qk", "具体怎么看呢？让“它”拿着自己的 Query，去和每个词的 Key 做点积。",
                "具体怎么看呢？让“它”拿着自己的Query，去和每个词的Key做点积。"),
    ChapterLine("match", "点积越大，说明越匹配——这里“牛奶”的分数最高。"),
    ChapterLine("scale", "可是分数太大的话，softmax 会把几乎全部权重都压给一个词。",
                "可是分数太大的话，softmax会把几乎全部权重都压给一个词。"),
    ChapterLine("sqrt", "所以先除以 √d（这里 d = 64，√d = 8），把分数拉回温和的范围。",
                "所以先除以根号d，这里d等于64，根号d就是8，把分数拉回温和的范围。"),
    ChapterLine("sum", "最后按这些权重，把每个词的 Value 加起来，“它”的新向量里就带上了牛奶的信息。",
                "最后按这些权重，把每个词的Value加起来，“它”的新向量里就带上了牛奶的信息。"),
]


def director_js() -> str:
    return Path(__file__).with_name("attention.js").read_text(encoding="utf-8")
