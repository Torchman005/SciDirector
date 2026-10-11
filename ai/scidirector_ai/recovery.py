"""Bounded recovery instructions derived from machine evidence, not user expertise."""
from __future__ import annotations

from .schemas import ShotSpec, StyleGuide


def recovery_instruction(shot: ShotSpec, style: StyleGuide, *, rebuild: bool) -> str:
    lines = [
        "系统负责诊断与修复，用户意见仅说明期望，不要求用户提供技术方案。",
        "根据实际异常、源码和未关闭的审核问题定位根因；不能原样重试，也不能只改配色或说明。",
        "先核对旁白、图示和公式的科学含义。visual_brief/beats 是可能出错的画面草案；"
        "若类比、函数曲线或因果关系与科学定义冲突，应修正表达，保留旁白的事实和限定条件。"
        "不能为了遵循错误草案，把多输入运算画成无依据的单变量曲线。",
    ]
    if rebuild:
        lines += ["本轮采用完整重建：输出完整场景/源码，重新分配对象、连接和时间轴，不输出局部补丁。",
                  "对拥挤画面按旁白顺序分阶段揭示，后续阶段替换已讲完的内容；"
                  "保留机制、关键步骤与必要标注，禁止退化成字幕卡、空背景或删除科学内容来通过审核。"]
    else:
        lines.append("本轮定点修复：保留已正确部分，修改错误涉及的对象与关联连接。")
    if style.presenter_safe_area:
        area = style.presenter_safe_area
        lines.append(f"讲解员局部避让矩形为归一化 x={area.x:g}～{area.x+area.width:g}、"
                     f"y={area.y:g}～{area.y+area.height:g}。"
                     "与它相交的文字/图形移至该矩形上方或左方，连接端点随对象移动；"
                     "保留文字字号，不缩整幅画面，不留整列角色栏。")
    if shot.engine and shot.engine.value == "manim":
        lines.append("Manim 的 wait/play/run_time 必须为正数；无剩余时长就省略末尾 wait。"
                     "如复杂曲线/API 持续报错，用基础图元与显式数学运算重建等价机制，禁止虚构曲线。")
    return "\n".join(lines)
