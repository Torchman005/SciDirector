"""A bounded textual science check, independent of a VLM's visual verdict."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from .llm import LLMClient, Task
from .schemas import CriticFeedback, RepairTask, ShotSpec
from .science_evidence import manim_review_manifest


class ScienceFinding(BaseModel):
    verdict: Literal["correct", "incorrect", "uncertain"]
    target: str
    evidence: str
    instruction: str = ""
    acceptance: str = ""
    repair_scope: Literal["visual", "narration", "both"] = "visual"


class ScienceResolution(BaseModel):
    task_id: str
    resolved: bool
    evidence: str


class ScienceAudit(BaseModel):
    findings: list[ScienceFinding] = Field(min_length=1)
    resolutions: list[ScienceResolution] = Field(default_factory=list)


def science_request(shot: ShotSpec, previous: CriticFeedback | None) -> str:
    if not shot.engine or shot.engine.value != "manim":
        return ""
    manifest = manim_review_manifest(shot.code)
    if not manifest:
        return ""
    data = json.loads(manifest[manifest.index('{'):])
    # The previous diagnosis describes an OLD source. Passing its quoted wrong
    # formula/"missing label" as current evidence made the auditor repeat a
    # disproven finding. Retain obligations, not stale observations.
    old = [{'task_id': t.task_id, 'target': t.target, 'acceptance': t.acceptance, 'repair_scope': t.repair_scope}
           for t in (previous.repair_tasks if previous else [])
           if t.task_id.startswith('science-') and t.status != 'resolved']
    if not old and not data['mathematical_source'] and not any(t['kind'] in {'Tex', 'MathTex'} for t in data['literal_text']):
        return ""
    return json.dumps({'narration': shot.narration,
                       'source_evidence': data, 'previous_science_tasks': old}, ensure_ascii=False)


def audit_science(llm: LLMClient, request: str) -> ScienceAudit:
    from .agents.base import load_prompt
    return llm.chat_json(load_prompt('science_audit'), request, ScienceAudit,
                         task=Task.SCIENCE, temperature=0, max_tokens=2600, max_parse_attempts=1)


def science_tasks(audit: ScienceAudit, *, attempt: int, duration: float) -> list[RepairTask]:
    tasks = []
    for finding in audit.findings:
        if finding.verdict != 'incorrect':
            continue
        # Empty/vague diagnoses cannot become invented repair work.
        if not all(s.strip() for s in (finding.target, finding.evidence, finding.instruction, finding.acceptance)):
            raise ValueError('科学核对指出错误但缺少对象、证据或验收条件')
        tasks.append(RepairTask(task_id=f'science-{attempt}-{len(tasks)+1:02}', category='logic',
            severity='blocking', start_sec=0, end_sec=duration, target=finding.target,
            evidence='源码定义核对：'+finding.evidence, instruction=finding.instruction, acceptance=finding.acceptance,
            repair_scope=finding.repair_scope))
    return tasks[:3]


def resolve_science(previous: CriticFeedback | None, audit: ScienceAudit) -> CriticFeedback | None:
    if not previous:
        return previous
    resolutions = {r.task_id: r for r in audit.resolutions if r.resolved and r.evidence.strip()}
    incorrect = {f.target for f in audit.findings if f.verdict == 'incorrect'}
    return previous.model_copy(update={'repair_tasks': [
        task.model_copy(update={'status': 'resolved', 'resolution_evidence':
            '当前源码与原验收条件重新核对：'+resolutions[task.task_id].evidence})
        if task.task_id.startswith('science-') and task.task_id in resolutions and task.target not in incorrect else task
        for task in previous.repair_tasks]})
