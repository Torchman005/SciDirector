"""Shared, deterministic repair selection and formatting for automatic and HITL calls."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from .schemas import CriticFeedback, RepairTask

# A small repair batch keeps the coder focused and makes each review measurable.
MAX_REPAIRS_PER_ROUND = 3
FAILURES_BEFORE_ESCALATION = 1
FAILURES_BEFORE_HANDOFF = 2
# 台账上限：VLM 每轮换说法会不断产生"新"任务，不设上限的台账会无限膨胀并永久阻塞。
MAX_LEDGER_TASKS = 16
# 文本相似度阈值：同类问题在两轮审核里的措辞往往只改了几个字。
SIMILARITY_THRESHOLD = 0.6
# 时间段/帧重合时，只需较弱的文本相似度即可视为同一问题（避免同段两个不同问题被合并）。
OVERLAP_SIMILARITY_THRESHOLD = 0.3
HUMAN_TASK_PREFIX = "human-"
STALE_RESOLUTION = "复审未再观察到该问题"
SEVERITY_ORDER = {"blocking": 0, "major": 1, "advisory": 2}
REPAIR_STRATEGIES = {
    "logic": "重构解释顺序与图示关系，先呈现前提，再展示推导与结论，逐项核对旁白",
    "readability": "重新分配文字区域，减少同屏文字并提高主次对比，避免反复只微调字号",
    "layout": "重新布局相关对象，为标签预留独立空间，消除重叠和裁切",
    "pacing": "把问题时段拆成连续可见的讲解阶段，按旁白顺序逐段推进，避免只延长等待",
    "rendering": "简化问题元素的实现，替换不可靠资源或效果，先保证关键内容正确可见",
    "human": "逐条落实审核员意见，必要时重新设计相关画面阶段，并在说明中写明对应修改",
}

_ELEMENT_ID = re.compile(r"#([A-Za-z_][\w\-]*)")


def is_human_task(task: RepairTask) -> bool:
    return task.category == "human" or task.task_id.startswith(HUMAN_TASK_PREFIX)


def is_science_task(task: RepairTask) -> bool:
    return task.task_id.startswith("science-")


def task_blocks(task: RepairTask, *, score_ok: bool) -> bool:
    """未关闭任务是否阻止通过。

    blocking 一律阻止；major 只在分数未达标、或属于人工/科学/逻辑问题时阻止；
    advisory（审美偏好等）永远只作建议。
    """
    if task.status == "resolved" or task.severity == "advisory":
        return False
    if task.severity == "blocking" or is_human_task(task) or is_science_task(task):
        return True
    return task.category == "logic" or not score_ok


def human_repair_task(comment: str, *, attempt: int, index: int = 1, duration: float = 0.0,
                      start_sec: float = 0.0, end_sec: float = 0.0) -> RepairTask:
    """把审核员的打回意见转为阻塞任务；只有逐项复审 resolved 或人工放行才能关闭。"""
    text = " ".join(comment.split())
    ranged = end_sec > start_sec
    return RepairTask(
        task_id=f"{HUMAN_TASK_PREFIX}{attempt}-{index:02d}", category="human", severity="blocking",
        start_sec=start_sec if ranged else 0.0, end_sec=end_sec if ranged else max(duration, 0.0),
        target="审核员打回意见", evidence="审核员意见：" + text, instruction=text,
        acceptance="当前画面已落实审核员意见：" + text,
    )


def ensure_human_task(feedback: CriticFeedback | None, comment: str, *, attempt: int,
                      duration: float = 0.0, start_sec: float = 0.0, end_sec: float = 0.0,
                      ) -> CriticFeedback | None:
    """保证本次人工意见存在于台账；Go 已持久化过的同一意见不重复登记。"""
    text = " ".join((comment or "").split())
    if not text or text.startswith("自动诊断并修复："):
        return feedback
    tasks = list(feedback.repair_tasks) if feedback else []
    humans = [t for t in tasks if is_human_task(t)]
    if any(" ".join(t.instruction.split()) == text for t in humans):
        return feedback
    task = human_repair_task(text, attempt=attempt, index=len(humans) + 1, duration=duration,
                             start_sec=start_sec, end_sec=end_sec)
    while any(t.task_id == task.task_id for t in tasks):
        task.task_id += "x"
    if feedback is None:
        return CriticFeedback(passed=False, kind="repair_request", repair_tasks=[task],
                              suggestions=[task.instruction])
    return feedback.model_copy(update={"passed": False, "repair_tasks": tasks + [task]})


def update_repair_progress(
    feedback: CriticFeedback | None, progress: dict[str, Any], attempt: int, max_attempts: int,
) -> tuple[dict[str, Any], list[RepairTask], bool]:
    """The first sighting is a baseline; stop after two ineffective repairs.

    只有复审再次观察到问题（open）或重复的 partial 才算一次失败；
    unverified 表示"没拿到有效证据"，是中性的，不能把镜头推向人工。
    """
    updated = {k: dict(v) for k, v in progress.items()}
    selected = active_repairs(feedback)
    escalations = []
    exhausted = False
    for task in selected:
        if task.task_id not in updated:
            updated[task.task_id] = {"failures": 0, "last_attempt": attempt,
                                     "last_status": task.status, "escalated_at": 0,
                                     "partial_seen": task.status == "partial"}
            continue
        row = updated[task.task_id]
        if row["last_attempt"] < attempt:
            seen = row.get("partial_seen", row.get("last_status") == "partial")
            # A first partial resolution earns time; repeated partial claims are not endless progress.
            if task.status == "unverified":
                pass
            elif task.status == "partial" and not seen:
                row["failures"] = 0
            else:
                row["failures"] += 1
            row["partial_seen"] = seen or task.status == "partial"
            row["last_attempt"], row["last_status"] = attempt, task.status
        if row["failures"] >= FAILURES_BEFORE_HANDOFF:
            exhausted = True
        elif row["failures"] >= FAILURES_BEFORE_ESCALATION and attempt < max_attempts and not row["escalated_at"]:
            row["escalated_at"] = attempt + 1
            escalations.append(task)
    return updated, escalations, exhausted


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def _element_ids(task: RepairTask) -> set[str]:
    return {m.casefold() for m in _ELEMENT_ID.findall(task.target)}


def _similarity(a: RepairTask, b: RepairTask) -> float:
    return SequenceMatcher(None, _norm(a.target + a.acceptance), _norm(b.target + b.acceptance)).ratio()


def _overlaps(a: RepairTask, b: RepairTask) -> bool:
    if set(a.frame_indices) & set(b.frame_indices):
        return True
    if a.end_sec > a.start_sec and b.end_sec > b.start_sec:
        inter = min(a.end_sec, b.end_sec) - max(a.start_sec, b.start_sec)
        union = max(a.end_sec, b.end_sec) - min(a.start_sec, b.start_sec)
        return union > 0 and inter / union >= 0.5
    return False


def same_issue(a: RepairTask, b: RepairTask) -> bool:
    """同一缺陷的跨轮识别：VLM 每轮都会改写验收措辞，精确字符串匹配会让旧任务永远挂起。"""
    if a.category != b.category or is_human_task(a) != is_human_task(b):
        return False
    if (_norm(a.target), _norm(a.acceptance)) == (_norm(b.target), _norm(b.acceptance)):
        return True
    ids_a, ids_b = _element_ids(a), _element_ids(b)
    if ids_a and ids_b:
        return bool(ids_a & ids_b)
    ratio = _similarity(a, b)
    if _overlaps(a, b) and ratio >= OVERLAP_SIMILARITY_THRESHOLD:
        return True
    return ratio >= SIMILARITY_THRESHOLD


def carry_repairs(previous: CriticFeedback | None, current: CriticFeedback) -> CriticFeedback:
    """An unavailable review cannot change a previously verified task status."""
    pairs = {t.task_id: t.evidence_pairs for t in current.repair_tasks if t.evidence_pairs}
    return current.model_copy(update={
        "passed": False,
        "repair_tasks": [t.model_copy(deep=True, update={"evidence_pairs": pairs.get(t.task_id, t.evidence_pairs)})
                         for t in previous.repair_tasks] if previous else [],
    })


def repair_improved(previous: CriticFeedback | None, current: CriticFeedback) -> bool:
    """Only task-specific closure or a first partial result earns further repairs."""
    old = {t.task_id: t.status for t in previous.repair_tasks} if previous else {}
    return any(
        t.task_id in old and (
            (t.status == "resolved" and old[t.task_id] != "resolved")
            or (t.status == "partial" and old[t.task_id] not in {"partial", "resolved"})
        ) for t in current.repair_tasks
    )


def repair_stalled(previous: CriticFeedback | None, current: CriticFeedback) -> bool:
    """只有原任务被复审再次观察到（open）且没有任何改善，才算一次停滞；unverified 是中性的。"""
    if repair_improved(previous, current):
        return False
    shown = {t.task_id for t in active_repairs(previous)}
    return any(t.task_id in shown and t.status == "open" for t in current.repair_tasks)


def _in_range(task: RepairTask, review_range: tuple[float, float] | None) -> bool:
    if review_range is None:
        return True
    start, end = review_range
    if task.end_sec > task.start_sec:
        return task.start_sec < end and task.end_sec > start
    return True


def merge_repairs(
    previous: CriticFeedback | None, current: CriticFeedback,
    results: list[dict[str, Any]], verified_ids: set[str], *,
    score_ok: bool | None = None, review_range: tuple[float, float] | None = None,
) -> CriticFeedback:
    """Only explicit evidence closes a selected task; its acceptance remains immutable.

    - 复审再次观察到同一问题（按元素 id / 时间段 / 文本相似度匹配）-> open；
    - 有当前画面证据的逐项结论 -> 采用该结论；
    - 分数与下限达标且未再报告该问题 -> 视为已解决（人工与科学任务除外）；
    - 其余 -> unverified（中性，不计失败）。
    """
    score_ok = current.passed if score_ok is None else score_ok
    old = previous.repair_tasks if previous else []
    result_map = {r["task_id"]: r for r in results}
    fresh = [t for t in current.repair_tasks]
    ledger: list[RepairTask] = []
    for task in old:
        task = task.model_copy()
        if task.status == "resolved":
            # A current witnessed defect overrides a conflicting claim of resolution.
            match = next((n for n in fresh if same_issue(task, n)), None)
            if match is not None:
                fresh.remove(match)
                task.status = "open"
                task.resolution_evidence = "当前审核再次发现同一问题：" + match.evidence
            ledger.append(task)
            continue
        if not _in_range(task, review_range):
            ledger.append(task)
            continue
        match = next((n for n in fresh if same_issue(task, n)), None)
        result = result_map.get(task.task_id)
        if match is not None:
            fresh.remove(match)
            if not task.repair_scope:
                task.repair_scope = match.repair_scope
            task.status, task.resolution_evidence = "open", "当前审核再次发现同一问题：" + match.evidence
        elif result and task.task_id in verified_ids:
            task.status, task.resolution_evidence = result["status"], result["evidence"]
        elif is_human_task(task) or is_science_task(task):
            # 人工意见只能由逐项复审显式关闭；科学任务只能由源码核对关闭。
            if is_human_task(task) and result:
                task.status = "unverified"
                task.resolution_evidence = "复审未给出当前画面证据：" + str(result.get("evidence", ""))[:200]
        elif score_ok and current.kind in {"", "content_review"} and (
                not result or result.get("status") in {"resolved", "unverified"}):
            # 复审明确说"仍未解决"时即使缺图像编号也不能按"未再观察到"关闭。
            task.status, task.resolution_evidence = "resolved", STALE_RESOLUTION
        else:
            task.status, task.resolution_evidence = "unverified", "未获得针对原验收条件的有效复审证据"
        ledger.append(task)
    ledger.extend(fresh)
    ledger = _cap_ledger(ledger)
    required = [t for t in ledger if task_blocks(t, score_ok=score_ok)]
    passed = current.passed and not required
    return current.model_copy(update={
        "repair_tasks": ledger, "passed": passed,
        "suggestions": (current.suggestions or [t.instruction for t in required]
                        or [t.instruction for t in ledger if t.status != "resolved"][:3]) if not passed else [],
    })


def _cap_ledger(ledger: list[RepairTask]) -> list[RepairTask]:
    if len(ledger) <= MAX_LEDGER_TASKS:
        return ledger
    # 优先丢弃已解决的旧任务，其次是建议级任务；人工与阻塞任务永远保留。
    def keep_rank(item: tuple[int, RepairTask]) -> tuple[int, int]:
        index, task = item
        if is_human_task(task) or is_science_task(task) or task.severity == "blocking":
            rank = 0
        elif task.status != "resolved" and task.severity != "advisory":
            rank = 1
        elif task.status != "resolved":
            rank = 2
        else:
            rank = 3
        return rank, -index
    kept = sorted(enumerate(ledger), key=keep_rank)[:MAX_LEDGER_TASKS]
    return [task for _, task in sorted(kept, key=lambda item: item[0])]


def active_repairs(feedback: CriticFeedback | None) -> list[RepairTask]:
    if feedback is None or feedback.passed:
        return []
    candidates = [task for task in feedback.repair_tasks
                  if task.status != "resolved" and task.severity != "advisory" and task.actionable]
    humans = [task for task in candidates if is_human_task(task)]
    others = sorted((task for task in candidates if not is_human_task(task)),
                    key=lambda task: SEVERITY_ORDER[task.severity])
    # 人工意见必须每轮都进入清单；机器任务填满剩余名额。
    return humans + others[:max(MAX_REPAIRS_PER_ROUND - len(humans), 1 if not humans else 0)]


def _format_task(task: RepairTask) -> str:
    location = (f"{task.start_sec:.2f}～{task.end_sec:.2f} 秒"
                if task.end_sec > task.start_sec else "")
    if task.frame_indices:
        location += f" 第 {','.join(map(str, task.frame_indices))} 帧"
    return (f"- [{task.task_id}] {task.severity} / {task.category} / {location.strip()}\n"
            f"  对象：{task.target}\n  证据：{task.evidence}\n"
            f"  修改：{task.instruction}\n  验收：{task.acceptance}")


def format_repairs(feedback: CriticFeedback | None) -> str:
    tasks = active_repairs(feedback)
    if not tasks:
        return ""
    humans = [task for task in tasks if is_human_task(task)]
    machine = [task for task in tasks if not is_human_task(task)]
    lines: list[str] = []
    if humans:
        lines.append("【审核员意见（优先级最高，必须逐条落实；可为此调整相关画面阶段与布局）】")
        lines += [_format_task(task) for task in humans]
    if machine:
        if humans:
            lines.append("【同时修复的机器审核任务（按严重度排序）】")
            lines.append("在落实审核员意见的前提下修复以下问题，保留已正确的内容。")
        else:
            lines.append("【本轮修复任务（最多 3 项，按严重度排序）】")
            lines.append("只修改这些问题涉及的元素或阶段，保留已正确的内容。")
        lines += [_format_task(task) for task in machine]
    return "\n".join(lines)
