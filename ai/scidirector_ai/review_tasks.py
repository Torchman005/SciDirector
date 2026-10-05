"""Shared, deterministic repair selection and formatting for automatic and HITL calls."""

from __future__ import annotations

from .schemas import CriticFeedback, RepairTask

# A small repair batch keeps the coder focused and makes each review measurable.
MAX_REPAIRS_PER_ROUND = 3
SEVERITY_ORDER = {"blocking": 0, "major": 1, "advisory": 2}


def active_repairs(feedback: CriticFeedback | None) -> list[RepairTask]:
    if feedback is None or feedback.passed:
        return []
    return sorted(
        (task for task in feedback.repair_tasks
         if task.status != "resolved" and task.severity != "advisory" and task.actionable),
        key=lambda task: SEVERITY_ORDER[task.severity],
    )[:MAX_REPAIRS_PER_ROUND]


def format_repairs(feedback: CriticFeedback | None) -> str:
    tasks = active_repairs(feedback)
    if not tasks:
        return ""
    lines = ["【本轮修复任务（最多 3 项，按严重度排序）】",
             "只修改这些问题涉及的元素或阶段，保留已正确的内容。"]
    for task in tasks:
        location = (f"{task.start_sec:.2f}～{task.end_sec:.2f} 秒"
                    if task.end_sec > task.start_sec else "")
        if task.frame_indices:
            location += f" 第 {','.join(map(str, task.frame_indices))} 帧"
        lines.append(
            f"- [{task.task_id}] {task.severity} / {task.category} / {location.strip()}\n"
            f"  对象：{task.target}\n  证据：{task.evidence}\n"
            f"  修改：{task.instruction}\n  验收：{task.acceptance}"
        )
    return "\n".join(lines)
