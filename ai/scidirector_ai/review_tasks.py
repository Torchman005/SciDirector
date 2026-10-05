"""Shared, deterministic repair selection and formatting for automatic and HITL calls."""

from __future__ import annotations

import re
from typing import Any

from .schemas import CriticFeedback, RepairTask

# A small repair batch keeps the coder focused and makes each review measurable.
MAX_REPAIRS_PER_ROUND = 3
FAILURES_BEFORE_ESCALATION = 2
SEVERITY_ORDER = {"blocking": 0, "major": 1, "advisory": 2}
REPAIR_STRATEGIES = {
    "logic": "重构解释顺序与图示关系，先呈现前提，再展示推导与结论，逐项核对旁白",
    "readability": "重新分配文字区域，减少同屏文字并提高主次对比，避免反复只微调字号",
    "layout": "重新布局相关对象，为标签预留独立空间，消除重叠和裁切",
    "pacing": "把问题时段拆成连续可见的讲解阶段，按旁白顺序逐段推进，避免只延长等待",
    "rendering": "简化问题元素的实现，替换不可靠资源或效果，先保证关键内容正确可见",
}


def update_repair_progress(
    feedback: CriticFeedback | None, progress: dict[str, Any], attempt: int, max_attempts: int,
) -> tuple[dict[str, Any], list[RepairTask], bool]:
    """Count once per attempt and grant at most one escalation before human handoff."""
    updated = {k: dict(v) for k, v in progress.items()}
    selected = active_repairs(feedback)
    escalations = []
    exhausted = False
    for task in selected:
        row = updated.setdefault(task.task_id, {"failures": 0, "last_attempt": 0, "escalated_at": 0})
        if row["last_attempt"] < attempt:
            # A first partial resolution earns time; repeated partial claims are not endless progress.
            improved = task.status == "partial" and row.get("last_status") != "partial"
            row["failures"] = 0 if improved else row["failures"] + 1
            row["last_attempt"], row["last_status"] = attempt, task.status
        if row["escalated_at"] and attempt >= row["escalated_at"] and row["failures"] >= FAILURES_BEFORE_ESCALATION:
            exhausted = True
        elif row["failures"] >= FAILURES_BEFORE_ESCALATION and attempt < max_attempts and not row["escalated_at"]:
            row["escalated_at"] = attempt + 1
            escalations.append(task)
    return updated, escalations, exhausted


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
