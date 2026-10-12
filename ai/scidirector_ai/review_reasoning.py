"""Bounded review of a failing VLM's reasoning, never a substitute for images."""
import json
from pydantic import BaseModel, Field

from .llm import Task


class ReviewDispute(BaseModel):
    claim: str = Field(min_length=1)
    correction: str = Field(min_length=1)


class ReviewReasonCheck(BaseModel):
    disputes: list[ReviewDispute] = Field(default_factory=list, max_length=3)


def check_review_reasoning(llm, current_source: str, critique: str) -> ReviewReasonCheck:
    from .agents.base import load_prompt
    data = json.loads(critique)
    logic_tasks = [t for t in data.get('repair_tasks', [])
                   if t.get('category') == 'logic' and t.get('status') != 'resolved']
    claim = {'logic_score': data.get('logic_score'), 'logic_tasks': logic_tasks}
    if not logic_tasks:
        claim['issues'] = data.get('issues', [])
    return llm.chat_json(load_prompt('review_reasoning'),
        '当前源码与旁白：\n' + current_source + '\n待复核的逻辑判负理由：\n' + json.dumps(claim,ensure_ascii=False),
        ReviewReasonCheck, task=Task.REVIEW_REASONING, temperature=0,
        max_tokens=1400, max_parse_attempts=1)
