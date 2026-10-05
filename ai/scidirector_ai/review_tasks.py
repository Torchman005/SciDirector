"""Shared, deterministic repair selection and formatting for automatic and HITL calls."""

from __future__ import annotations

import re
from typing import Any

from .schemas import CriticFeedback, RepairTask

# A small repair batch keeps the coder focused and makes each review measurable.
MAX_REPAIRS_PER_ROUND = 3
SEVERITY_ORDER = {"blocking": 0, "major": 1, "advisory": 2}


def _identity(task: RepairTask) -> tuple[str, str, str]:
    return (task.category, re.sub(r"\s+", "", task.target).casefold(),
            re.sub(r"\s+", "", task.acceptance).casefold())


def merge_repairs(
    previous: CriticFeedback | None, current: CriticFeedback,
    results: list[dict[str, Any]], verified_ids: set[str],
) -> CriticFeedback:
    """Only explicit evidence closes a selected task; its acceptance remains immutable."""
    old = previous.repair_tasks if previous else []
    selected = {t.task_id for t in active_repairs(previous)}
    result_map = {r["task_id"]: r for r in results}
    ledger: list[RepairTask] = []
    for task in old:
        result = result_map.get(task.task_id)
        if task.task_id not in selected:
            ledger.append(task.model_copy())
            continue
        status, evidence = "unverified", "未获得针对原验收条件的有效复审证据"
        if result and task.task_id in verified_ids:
            status, evidence = result["status"], result["evidence"]
        ledger.append(task.model_copy(update={"status": status, "resolution_evidence": evidence}))
    for new in current.repair_tasks:
        match = next((t for t in ledger if _identity(t) == _identity(new)), None)
        if match:
            # A current witnessed defect overrides a conflicting claim of resolution.
            if match.status == "resolved":
                match.status = "open"
                match.resolution_evidence = "当前审核再次发现同一问题：" + new.evidence
            continue
        ledger.append(new)
    required = [t for t in ledger if t.status != "resolved" and t.severity != "advisory"]
    passed = current.passed and not required
    return current.model_copy(update={
        "repair_tasks": ledger, "passed": passed,
        "suggestions": (current.suggestions or [t.instruction for t in required]) if not passed else [],
    })


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
