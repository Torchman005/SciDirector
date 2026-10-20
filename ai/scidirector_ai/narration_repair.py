"""Evidence-bound script edits; neither the editor nor its verifier closes tasks."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .agents.base import load_prompt
from .llm import LLMClient, LLMError, Task
from .review_tasks import MAX_REPAIRS_PER_ROUND
from .schemas import CriticFeedback, NarrationRevision, RepairTask, ShotSpec


class NarrationRepairError(ValueError):
    """Stop safely when a claimed script defect cannot be corrected with evidence."""


class NarrationEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    before: str = Field(min_length=1, max_length=2000)
    after: str = Field(min_length=1, max_length=2000)


class NarrationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["revise", "unchanged", "uncertain"]
    reason: str = Field(min_length=1)
    edits: list[NarrationEdit] = Field(default_factory=list, max_length=MAX_REPAIRS_PER_ROUND)


class NarrationVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["correct", "incorrect", "uncertain"]
    reason: str = Field(min_length=1)


def narration_tasks(feedback: CriticFeedback | None) -> list[RepairTask]:
    if feedback is None or feedback.passed:
        return []
    return [t for t in feedback.repair_tasks
            if t.status != "resolved" and t.severity != "advisory" and t.actionable
            and t.category == "logic"
            and (t.repair_scope in {"narration", "both"}
                 or (not t.repair_scope and t.target.strip().casefold() in {"narration", "旁白", "画外音"}))][:MAX_REPAIRS_PER_ROUND]


def repair_narration(llm: LLMClient, shot: ShotSpec, feedback: CriticFeedback | None,
                     *, attempt: int, partial: bool = False) -> ShotSpec:
    tasks = narration_tasks(feedback)
    if not tasks:
        return shot
    verified_ids = {task_id for revision in shot.narration_revisions
                    if revision.after == shot.narration for task_id in revision.task_ids}
    tasks = [task for task in tasks if task.task_id not in verified_ids]
    if not tasks:
        return shot
    if partial:
        raise NarrationRepairError("旁白修订需要整镜重做，不能复用局部修补的旧音轨")
    data = {"narration": shot.narration, "tasks": [dict(task_id=t.task_id, target=t.target,
        evidence=t.evidence, instruction=t.instruction, acceptance=t.acceptance) for t in tasks]}
    try:
        plan = llm.chat_json(load_prompt("narration_repair"), json.dumps(data, ensure_ascii=False),
            NarrationPlan, task=Task.NARRATION_REPAIR, temperature=0, max_tokens=2200, max_parse_attempts=1)
        if plan.verdict != "revise" or not plan.edits or not plan.reason.strip():
            raise NarrationRepairError("旁白错误尚未获得可验证修法：" + plan.reason)
        allowed = {t.task_id for t in tasks}
        spans = []
        for edit in plan.edits:
            if edit.task_id not in allowed or not edit.before.strip() or not edit.after.strip():
                raise NarrationRepairError("旁白修订没有关联原任务或试图清空内容")
            if shot.narration.count(edit.before) != 1 or edit.before == edit.after:
                raise NarrationRepairError("旁白修订原句无法唯一定位或没有实际变化")
            start = shot.narration.index(edit.before)
            spans.append((start, start + len(edit.before), edit.after))
        spans.sort()
        if any(left[1] > right[0] for left, right in zip(spans, spans[1:])):
            raise NarrationRepairError("旁白修订范围互相重叠")
        corrected = shot.narration
        for start, end, replacement in reversed(spans):
            corrected = corrected[:start] + replacement + corrected[end:]
        verified = llm.chat_json(load_prompt("narration_verify"), json.dumps({
            **data, "corrected_narration": corrected,
            "edits": [e.model_dump() for e in plan.edits]}, ensure_ascii=False), NarrationVerification,
            task=Task.NARRATION_VERIFY, temperature=0, max_tokens=1400, max_parse_attempts=1)
        if verified.verdict != "correct" or not verified.reason.strip():
            raise NarrationRepairError("旁白独立核对未通过：" + verified.reason)
    except (LLMError, ValueError) as exc:
        if isinstance(exc, NarrationRepairError):
            raise
        raise NarrationRepairError("旁白修订或独立核对不可用：" + str(exc)[:500]) from exc
    revision = NarrationRevision(before=shot.narration, after=corrected,
        reason=plan.reason + "；独立核对：" + verified.reason,
        task_ids=list(dict.fromkeys(e.task_id for e in plan.edits)), attempt=attempt)
    meta = {k: v for k, v in shot.meta.items() if k != "overlay_text"}
    meta["repair_strategy"] = "rebuild"
    return shot.model_copy(update={"narration": corrected, "narration_revisions": [*shot.narration_revisions, revision],
        "visual_brief": "按当前已修正的旁白重新设计机制图，保留原任务的科学验收条件；不要重用旧版错误比喻或曲线。",
        "beats": [], "keywords": [], "meta": meta})
