"""审查智能体：用视觉大模型（VLM）评估渲染出的画面帧。

这是闭环里**唯一能发现"画面不对"的环节** —— 渲染器只能告诉你
"视频产出成功"，无法告诉你"公式画错了"或"字号小得看不清"。

三条设计原则：

1. **阈值在代码里，不在提示词里。**
   提示词里写固定分数，模型会围绕边界给出自相矛盾的结论。
   因此模型只负责输出四个维度分与问题列表，**是否通过由程序计算**。

2. **模型只对有证据的致命问题投否决票，普通质量建议不能无限打回。**
   致命问题必须进入结构化 `fatal_issues`，并包含帧号或明确可见证据；
   其它节奏与审美意见进入建议，由程序化分数和重试闸门共同处理。

3. **VLM 不可用时降级转人工，绝不伪造"通过"。**
   伪造通过会把未审查的画面放进成片 —— 这是本项目最不能接受的失败形态。
"""

from __future__ import annotations

import re
import json
from pathlib import Path
from typing import Any, Literal
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..llm import LLMClient, LLMError, LLMParseError, Task
from ..media import frame_change_summary
from ..logging import get_logger
from ..review_tasks import active_repairs, merge_repairs
from ..scene_revision import review_manifest
from ..schemas import (
    CriticFeedback,
    FeedbackSource,
    RepairTask,
    RenderArtifact,
    ShotSpec,
    StyleGuide,
)
from .base import Agent, render_prompt, style_guide_to_text

logger = get_logger(__name__)

# 各维度在总分中的权重。
# **必须与 prompts/critic.md 里写的加权公式一致** —— 不一致会导致
# "模型按一套规则算分、程序按另一套判定"，表现为通过率莫名偏移且极难定位。
# tests/test_critic_prompt.py 校验提示词里的权重之和为 1，这里保证项与之一一对应。
DIMENSION_WEIGHTS: dict[str, float] = {
    "logic_score": 0.35,
    "readability_score": 0.30,
    "pacing_score": 0.20,
    "aesthetics_score": 0.15,
}

# 单维度的硬性下限：任一不达标即不通过，无论总分多高。
# 理由：总分是加权平均，一个 0.1 的可读性会被其他三项拉到阈值以上，
# 但"看不清"的画面没有任何交付价值。
DIMENSION_FLOORS: dict[str, float] = {
    "logic_score": 0.70,
    "readability_score": 0.60,
}

#: 维度名 -> 中文标签（用于人类可读的 issues）。
_DIMENSION_LABELS = {
    "logic_score": "逻辑一致性",
    "readability_score": "文字可读性",
    "pacing_score": "节奏",
    "aesthetics_score": "美观度",
}


class _RepairResult(BaseModel):
    task_id: str
    status: Literal["open", "partial", "resolved", "unverified"]
    evidence: str = ""
    image_indices: list[int] = Field(default_factory=list)


class _RawCritique(BaseModel):
    """模型直出的评审结果。

    字段必须与 ``prompts/critic.md`` 第五节的 JSON schema 完全一致
    （有契约测试守护）。保留模型自报的 ``passed`` 作为诊断信号；只有
    ``fatal_issues`` 中的可核对问题才有否决权。
    """

    passed: bool = False
    score: float = 0.0
    logic_score: float = 0.0
    readability_score: float = 0.0
    pacing_score: float = 0.0
    aesthetics_score: float = 0.0
    issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    # Only concrete rendering failures may veto a quantitatively passing shot.
    fatal_issues: list[str] = Field(default_factory=list)
    repair_tasks: list[RepairTask] = Field(default_factory=list)
    repair_results: list[_RepairResult] = Field(default_factory=list)


@dataclass
class CritiqueOutcome:
    """审查结论（含降级标记与诊断信息）。"""

    feedback: CriticFeedback
    #: 是否发生了降级（无法自动审查 -> 转人工）。
    degraded: bool = False
    degradation_reason: str = ""
    frames_reviewed: int = 0
    #: 模型自报的 passed 与程序判定的 passed（用于观测提示词质量）。
    model_passed: bool = False
    program_passed: bool = False
    raw_response: str = field(default="", repr=False)

    @property
    def passed(self) -> bool:
        return self.feedback.passed

    @property
    def verdict_disagreement(self) -> bool:
        """模型与程序的结论是否不一致。

        这不是错误，而是一个**提示词质量信号**：长期不一致说明
        提示词里的判定规则没有被模型正确执行，值得去看一眼。
        """
        return self.model_passed != self.program_passed


class CriticAgent(Agent):
    """审查智能体。"""

    prompt_name = "critic"

    def __init__(self, llm: LLMClient | None = None, settings: Settings | None = None) -> None:
        super().__init__(llm)
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def review(
        self,
        *,
        shot: ShotSpec,
        artifact: RenderArtifact,
        style_guide: StyleGuide,
        attempt: int,
        previous_feedback: str = "",
        previous_review: CriticFeedback | None = None,
        repair_prechecks: list[dict[str, Any]] | None = None,
    ) -> CritiqueOutcome:
        """审查一个渲染产物。

        ``artifact.frame_samples`` 必须已由渲染阶段抽好
        （见 ``tools/media.extract_frames``）。没有抽帧时**直接降级转人工**，
        而不是盲判通过 —— 那等于把未审查的画面放进成片。
        """
        frames = [f for f in (artifact.frame_samples or []) if f]
        if not frames:
            return self._degrade(
                shot=shot,
                attempt=attempt,
                reason="渲染产物没有可用的抽帧，无法进行视觉审查",
            )

        # 容差下限：低于下限但仍在它的 90% 以内时，判"偏小但可读"而不是"不可读"。
        #
        # 为什么需要：差一两个像素就判负会触发**一整轮重渲染**（数十秒到数分钟），
        # 而重渲之后量到的值往往仍在同一档 —— 白烧成本、画面却没变好。
        # 真正该拦的是"明显读不清"，不是"比标准矮了一点"。
        min_font_size = int(style_guide.min_font_size)
        # 相邻抽帧的时间间隔 —— 必须显式告诉 VLM。
        #
        # 这条信息决定了它**有没有资格**判断连续运动。实测一个 22.83 秒的镜头
        # 只抽 4 张（默认 critic_frame_samples=4），间隔约 5.7 秒；而审查连续
        # 四轮都在要求"风扇持续旋转"（1.5 rad/s，约 4.2 秒转一圈）。
        # 从间隔 5.7 秒的静帧里**根本无法判断**转没转 —— 它却把这写成了判定理由，
        # 于是每一轮都判负、编码端每一轮都无法满足，一直烧到人工介入。
        #
        # 系统提示与用户提示都要给：系统提示里用它把"节奏问题的建议量级"
        # 说清楚（见 critic.md 第四节），用户提示里用它说明抽帧的疏密。
        frame_interval_sec = (
            artifact.duration_sec / max(len(frames) - 1, 1) if len(frames) > 1 else 0.0
        )
        interval_text = f"{frame_interval_sec:.1f}"
        system_prompt = render_prompt(
            "critic",
            threshold=f"{self.settings.critic_score_threshold:.2f}",
            background_color=style_guide.background_color,
            min_font_size=min_font_size,
            min_font_size_tolerance=max(int(min_font_size * 0.9), 1),
            # 时长与采样间隔是"评估节奏"的必要背景：没有它们，
            # 系统提示里关于"建议量级要配得上问题量级"的规则就没法给出具体数字。
            duration_sec=round(shot.duration_sec, 2),
            frame_interval_sec=interval_text,
        )
        # 缩略图换算比例：必须告诉 VLM 它在缩略图上量到的字号要乘多少才是成片字号。
        #
        # 不告诉它会出现很隐蔽的单位错配：提示词里的字号下限是**成片像素**，
        # 而它是在缩略图上用眼睛量的。1080p 缩到 1024 宽是 1.875:1，
        # 于是 32px 的下限在它眼里只有 17px，它会一路要求"提到 48px" ——
        # 每次都白烧一轮 1080p 渲染加一次模型调用。
        preview_width = max(int(self.settings.critic_frame_width), 1)
        preview_scale = max(int(artifact.width or 0), 1) / preview_width
        user_prompt = render_prompt(
            "critic_user",
            index=shot.index,
            tag=shot.tag.value,
            engine=shot.engine.value if shot.engine else "",
            duration_sec=round(shot.duration_sec, 2),
            actual_duration=round(artifact.duration_sec, 2),
            width=artifact.width,
            height=artifact.height,
            attempt=attempt,
            narration=shot.narration or "（无画外音）",
            visual_brief=shot.visual_brief or "（无明确视觉意图）",
            style_guide=style_guide_to_text(style_guide),
            previous_feedback=_format_previous(previous_feedback),
            frame_count=len(frames),
            frame_interval_sec=f"{frame_interval_sec:.1f}",
            preview_width=preview_width,
            preview_scale=f"{preview_scale:.2f}",
            frame_change_summary=_safe_frame_change_summary(frames),
        )

        user_prompt += review_manifest(shot.code)
        images = list(frames)
        current_indices = set(range(1, len(frames) + 1))
        paired_ids: set[str] = set()
        task_current_indices: dict[str, set[int]] = {}
        repair_context = []
        for task in active_repairs(previous_review):
            row = next((r for r in (repair_prechecks or []) if r["task_id"] == task.task_id), {})
            pairs = []
            for pair in row.get("pairs", [])[:3]:
                if not all(Path(pair[k]).is_file() for k in ("before", "after")):
                    continue
                images.extend([pair["before"], pair["after"]])
                current_indices.add(len(images))
                task_current_indices.setdefault(task.task_id, set()).add(len(images))
                pairs.append({"ts": pair["ts"], "before_image": len(images) - 1,
                              "after_image": len(images)})
            if pairs:
                paired_ids.add(task.task_id)
            repair_context.append({"task": task.model_dump(), "precheck": row.get("status", "unverified"),
                                   "evidence_pairs": pairs})
        if repair_context:
            user_prompt += "\n" + render_prompt("repair_review", current_count=len(frames),
                                                  repair_context=json.dumps(repair_context, ensure_ascii=False))

        try:
            parsed = self.llm.vision_json(
                system_prompt,
                user_prompt,
                _RawCritique,
                images=images,
                task=Task.CRITIQUE,
            )
        except (LLMError, LLMParseError) as exc:
            # 关键的降级点：模型不可用时**绝不**伪造通过。
            logger.warning(
                "VLM 审查失败，降级为人工复核",
                extra={"shot_id": shot.shot_id, "attempt": attempt, "error": str(exc)[:300]},
            )
            return self._degrade(
                shot=shot,
                attempt=attempt,
                reason=f"VLM 审查调用失败：{exc}",
                frames=len(frames),
            )

        assert isinstance(parsed, _RawCritique)
        feedback, program_passed = self._decide(parsed, shot=shot, attempt=attempt)
        needs_locations = not feedback.passed or any(t.severity != "advisory" for t in feedback.repair_tasks)
        invalid_new_tasks = any(
            t.end_sec > artifact.duration_sec or any(i > len(frames) for i in t.frame_indices)
            for t in feedback.repair_tasks if t.severity != "advisory"
        )
        if (invalid_new_tasks or (needs_locations and not _valid_task_locations(feedback.repair_tasks, artifact)
                                  and not active_repairs(previous_review))):
            # One bounded clarification, never a render retry with vague feedback.
            try:
                parsed = self.llm.vision_json(
                    system_prompt,
                    user_prompt + "\n上一份判负反馈缺少有效 repair_tasks。请重新输出完整 JSON，"
                    "补充真实帧号/时间段、具体对象、画面证据、修改指令与可验收条件。"
                    "不能用泛泛建议或编造定位补齐。上一份输出：" + str(parsed.model_dump()),
                    _RawCritique, images=images, task=Task.CRITIQUE,
                )
                feedback, program_passed = self._decide(parsed, shot=shot, attempt=attempt)
            except (LLMError, LLMParseError) as exc:
                return self._degrade(shot=shot, attempt=attempt,
                                     reason=f"审核意见定位补充失败：{exc}", frames=len(frames))
            if ((not feedback.passed or any(t.severity != "advisory" for t in feedback.repair_tasks))
                    and not _valid_task_locations(feedback.repair_tasks, artifact)
                    and (feedback.repair_tasks or not active_repairs(previous_review))):
                return self._degrade(shot=shot, attempt=attempt,
                                     reason="审核意见缺少有效定位、证据或验收条件，停止无目标重做",
                                     frames=len(frames))

        verified_ids = {
            r.task_id for r in parsed.repair_results
            if r.task_id in paired_ids and r.evidence.strip() and r.image_indices
            and all(i in current_indices for i in r.image_indices)
            and bool(set(r.image_indices) & task_current_indices.get(r.task_id, set()))
        }
        feedback = merge_repairs(previous_review, feedback,
                                 [r.model_dump() for r in parsed.repair_results], verified_ids)
        outcome = CritiqueOutcome(
            feedback=feedback,
            frames_reviewed=len(frames),
            model_passed=bool(parsed.passed),
            program_passed=feedback.passed,
            raw_response=str(parsed.model_dump())[:2000],
        )

        log_extra = {
            "shot_id": shot.shot_id,
            "attempt": attempt,
            "passed": feedback.passed,
            "score": round(feedback.score, 3),
            "issues": len(feedback.issues),
            "suggestions": len(feedback.suggestions),
            "model_passed": outcome.model_passed,
            "program_passed": outcome.program_passed,
        }
        if outcome.verdict_disagreement:
            # 不是错误，而是提示词质量信号：长期不一致说明提示词里的
            # 判定规则没有被模型正确执行。
            logger.warning("审查结论与程序判定不一致（提示词质量信号）", extra=log_extra)
        else:
            logger.info("审查完成", extra=log_extra)
        return outcome

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------

    def _decide(
        self, raw: _RawCritique, *, shot: ShotSpec, attempt: int
    ) -> tuple[CriticFeedback, bool]:
        """由程序计算最终结论，返回 (反馈, 程序判定是否通过)。

        **不信任模型自报的 score**；模型自报的 passed 只用于诊断，致命否决
        必须来自结构化且可核对的 ``fatal_issues``。
        """
        dims = {
            "logic_score": _clamp01(raw.logic_score),
            "readability_score": _clamp01(raw.readability_score),
            "pacing_score": _clamp01(raw.pacing_score),
            "aesthetics_score": _clamp01(raw.aesthetics_score),
        }
        # 总分以**程序加权**为准。四个维度全为 0（模型没给维度分）时才回退到
        # 模型自报的 score，避免把一个本来有效的评分直接抹成 0。
        weighted = sum(dims[k] * w for k, w in DIMENSION_WEIGHTS.items())
        score = weighted if weighted > 0 else _clamp01(raw.score)

        threshold = self.settings.critic_score_threshold
        floor_failures = [name for name, floor in DIMENSION_FLOORS.items() if dims[name] < floor]

        program_passed = score >= threshold and not floor_failures
        # A model veto is reserved for evidence of a fatal rendering/content failure.
        # Ordinary pacing or aesthetic disagreement is actionable feedback, but should
        # not repeatedly reject a shot whose measured score already clears the gate.
        fatal_issues = [
            issue.strip() for issue in raw.fatal_issues
            if issue and _is_fatal_issue(issue)
        ]
        model_veto = bool(fatal_issues)
        passed = program_passed and not model_veto
        if model_veto:
            score = min(score, 0.3)

        issues = [i.strip() for i in raw.issues if i and i.strip()]
        suggestions = _sanitize_suggestions(raw.suggestions)

        # 兜底一：不通过但没给建议 -> 从问题清单派生可执行指令。
        if not passed and not suggestions:
            suggestions = _derive_suggestions(issues)

        # 兜底二：仍然为空 -> 模型给了"不可执行"的反馈。
        # 此时**不伪造建议**，而是给出明确的人工介入指引（上层据此转人工）。
        if not passed and not suggestions:
            logger.warning(
                "审查未通过但模型未给出可执行建议",
                extra={"shot_id": shot.shot_id, "attempt": attempt, "issues": issues[:3]},
            )
            suggestions = ["模型未能给出可执行的修改建议，请人工检查该镜头并按需直接编辑或接受"]
            issues.append("自动审查反馈不可执行（缺少具体修改指令）")

        if floor_failures:
            issues.append(
                "维度未达硬性下限：" + "、".join(_DIMENSION_LABELS.get(f, f) for f in floor_failures)
            )

        repair_tasks = _normalize_repair_tasks(
            raw.repair_tasks, attempt=attempt,
        )
        blocking = [task for task in repair_tasks if task.severity == "blocking"]
        if blocking:
            passed = False
            if not suggestions:
                suggestions = [task.instruction for task in blocking]
        if verdict_mismatch(bool(raw.passed), program_passed):
            issues.append(
                f"模型自报通过但程序判定未通过（加权得分 {score:.2f}，阈值 {threshold:.2f}）"
            )
        elif program_passed and not raw.passed and not model_veto and not blocking:
            issues.append("模型自报未通过但未指出可核对的致命问题，已按量化评分放行")
        if model_veto:
            issues.append("发现致命问题：" + "；".join(dict.fromkeys(fatal_issues)))

        feedback = CriticFeedback(
            passed=passed,
            score=round(score, 4),
            issues=issues,
            fatal_issues=list(dict.fromkeys(fatal_issues)),
            # 通过时不给建议：下游若按"有建议即重做"处理，
            # 带着建议的通过会导致无限重做已经合格的镜头。
            suggestions=suggestions if not passed else [],
            repair_tasks=repair_tasks,
            model=self.settings.vision_target().model,
            source=FeedbackSource.VLM,
            attempt=attempt,
            **dims,
        )
        return feedback, program_passed

    def _degrade(
        self,
        *,
        shot: ShotSpec,
        attempt: int,
        reason: str,
        frames: int = 0,
    ) -> CritiqueOutcome:
        """降级：无法自动审查 -> 转人工。

        ``passed=False`` + ``source=SYSTEM``：既不会被当成"通过"放行，
        也不会被判为"内容不合格"（那会触发一轮无意义的重渲染）。
        上层依据 ``degraded`` 直接把镜头置为 AWAITING_HUMAN。
        """
        feedback = CriticFeedback(
            passed=False,
            score=0.0,
            issues=[reason],
            suggestions=[f"请人工复核该镜头：{reason}"],
            model=self.settings.vision_target().model,
            source=FeedbackSource.SYSTEM,
            attempt=attempt,
        )
        return CritiqueOutcome(
            feedback=feedback,
            degraded=True,
            degradation_reason=reason,
            frames_reviewed=frames,
            model_passed=False,
            program_passed=False,
        )


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def verdict_mismatch(model_passed: bool, program_passed: bool) -> bool:
    """模型自报通过、但程序判定不通过 —— 需要把原因写进 issues 让人看见。"""
    return bool(model_passed) and not program_passed


_FATAL_MARKERS = (
    "全黑", "全白", "空白", "乱码", "裁切到无法", "完全无关", "渲染失败",
    "无法辨认", "缺失字体", "连接失败", "视频为空",
)
_FRAME_EVIDENCE = re.compile(r"第\s*\d+\s*(?:[、,，]\s*\d+\s*)?帧|所有帧|整段画面")


def _is_fatal_issue(issue: str) -> bool:
    text = " ".join(issue.lower().split())
    return bool(_FRAME_EVIDENCE.search(text)) and any(marker in text for marker in _FATAL_MARKERS)


def _safe_frame_change_summary(frames: list[str]) -> str:
    try:
        return frame_change_summary(frames)
    except (OSError, ValueError, RuntimeError):
        return "抽帧无法读取，不能提供像素变化统计。"


def _normalize_repair_tasks(
    tasks: list[RepairTask],
    *,
    attempt: int,
) -> list[RepairTask]:
    """Do not invent evidence to convert legacy vague feedback into a repair."""
    return [task.model_copy(update={
        "task_id": f"r{attempt}-{index + 1:02d}", "status": "open",
        "resolution_evidence": "",
    }) for index, task in enumerate(tasks) if task.actionable]


def _valid_task_locations(tasks: list[RepairTask], artifact: RenderArtifact) -> bool:
    required = [task for task in tasks if task.severity != "advisory"]
    return bool(required) and all(
        task.actionable and task.end_sec <= artifact.duration_sec
        and all(index <= len(artifact.frame_samples) for index in task.frame_indices)
        for task in required
    )


#: 判定"不可执行"的关键词。这些词描述的是感受，不是可操作的修改。
_VAGUE_MARKERS = (
    "不好看", "不够好", "太丑", "很难看", "不行", "有问题", "需要改进",
    "可以更好", "不美观", "感觉不", "有点怪", "更有科技感", "更好看一点",
)

#: 可执行建议里常见的"动作词"。命中的建议一律保留。
_ACTION_MARKERS = (
    "改", "调", "加", "去掉", "删", "换", "缩", "放大", "缩小",
    "提", "降", "减", "增", "旋转", "居中", "对齐", "拆", "合并",
)


def _sanitize_suggestions(suggestions: list[str]) -> list[str]:
    """过滤掉不可执行的建议，保留具体指令。

    判定刻意宽松（只要不是纯感受词就保留）：宁可多留一条略有冗余的具体建议，
    也不要误杀有效信息 —— 后者会让重试必然失败。
    """
    out: list[str] = []
    seen: set[str] = set()
    for item in suggestions:
        text = (item or "").strip()
        if not text or text in seen:
            continue
        # 含数字、代码标识符、动作词的建议一律保留（大概率可执行）。
        if (
            any(ch.isdigit() for ch in text)
            or any(kw in text for kw in _ACTION_MARKERS)
            or "_" in text
        ):
            seen.add(text)
            out.append(text)
            continue
        if any(marker in text for marker in _VAGUE_MARKERS):
            logger.debug("丢弃不可执行的建议", extra={"suggestion": text[:80]})
            continue
        seen.add(text)
        out.append(text)
    return out[:6]


def _derive_suggestions(issues: list[str]) -> list[str]:
    """从问题描述里派生出可执行建议。

    做法是把"问题"改写成"指令"：中文里两者往往只差句式
    （"字号太小" -> "把字号调大"）。不完美，但比直接丢给人工好：
    多数情况下一轮重试就能修好。
    """
    derived: list[str] = []
    for issue in issues:
        text = issue.strip()
        if not text:
            continue
        if any(k in text for k in ("字号", "字体", "太小", "看不清")):
            derived.append(f"放大相关文字：{text}（把 font-size 提高至少 1.5 倍）")
        elif any(k in text for k in ("重叠", "挤", "过密")):
            derived.append(f"降低元素密度或加大间距：{text}（减少刻度数量 / 增加 padding）")
        elif any(k in text for k in ("太快", "来不及", "一闪")):
            derived.append(f"放慢动画：{text}（把 run_time 提高至少 1.5 倍）")
        elif any(k in text for k in ("超出", "裁", "溢出")):
            derived.append(f"把元素约束在画面内：{text}（使用 scale_to_fit_width 或调整布局）")
        elif any(k in text for k in ("对比", "深底深字", "底色")):
            derived.append(f"提高对比度：{text}（深色背景改用白色文字）")
        elif any(k in text for k in ("全黑", "全白", "空白", "静止", "没生效")):
            derived.append("检查动画是否被真正触发：确认 construct() 中的 play() 会执行且元素可见")
        elif any(k in text for k in ("乱码", "方块", "字体缺失")):
            derived.append("检查中文字体是否安装（Manim 用 Text 而非 Tex 渲染中文）")
        else:
            derived.append(f"根据审查意见调整：{text}")
    return derived[:6]


def _format_previous(previous: str) -> str:
    if not previous.strip():
        return ""
    return f"## 上一轮反馈（用于确认是否已修复）\n\n{previous.strip()}\n"


def _clamp01(value: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, v))
