"""审查智能体：用视觉大模型（VLM）评估渲染出的画面帧。

这是闭环里**唯一能发现"画面不对"的环节** —— 渲染器只能告诉你
"视频产出成功"，无法告诉你"公式画错了"或"字号小得看不清"。

三条设计原则：

1. **阈值在代码里，不在提示词里。**
   提示词里写"请按 0.75 判定"，模型会给出 0.74 并自称通过。
   因此模型只负责输出四个维度分与问题列表，**是否通过由程序计算**。

2. **模型说"不通过"时一律采信，模型说"通过"时还要过程序这一关。**
   两个方向的风险不对称：
   * 漏判（坏画面放行）→ 坏画面流进成片，**不可逆**；
   * 误判（好画面打回）→ 多烧一轮渲染，**可逆且可观测**。
   所以取"两者都为真才算通过"。反过来若模型长期过度严格，
   日志里会打出 model/program 不一致的告警，提示词调优时能立刻看到。

3. **VLM 不可用时降级转人工，绝不伪造"通过"。**
   伪造通过会把未审查的画面放进成片 —— 这是本项目最不能接受的失败形态。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..llm import LLMClient, LLMError, LLMParseError, Task
from ..logging import get_logger
from ..schemas import (
    CriticFeedback,
    FeedbackSource,
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


class _RawCritique(BaseModel):
    """模型直出的评审结果。

    字段必须与 ``prompts/critic.md`` 第五节的 JSON schema 完全一致
    （有契约测试守护）。保留模型自报的 ``passed``，但它只作为**否决票**，
    不能单独决定通过。
    """

    passed: bool = False
    score: float = 0.0
    logic_score: float = 0.0
    readability_score: float = 0.0
    pacing_score: float = 0.0
    aesthetics_score: float = 0.0
    issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


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

        system_prompt = render_prompt(
            "critic",
            threshold=f"{self.settings.critic_score_threshold:.2f}",
            background_color=style_guide.background_color,
            min_font_size=style_guide.min_font_size,
        )
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
        )

        try:
            parsed = self.llm.vision_json(
                system_prompt,
                user_prompt,
                _RawCritique,
                images=frames,
                model=self.settings.vlm_model,
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

        outcome = CritiqueOutcome(
            feedback=feedback,
            frames_reviewed=len(frames),
            model_passed=bool(parsed.passed),
            program_passed=program_passed,
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

        **不信任模型自报的 score**，但把它的 passed 当作否决票。
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
        # 模型说通过不算数；模型说不通过一律采信（两个方向的风险不对称）。
        passed = program_passed and bool(raw.passed)

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
        if verdict_mismatch(bool(raw.passed), program_passed):
            issues.append(
                f"模型自报通过但程序判定未通过（加权得分 {score:.2f}，阈值 {threshold:.2f}）"
            )

        feedback = CriticFeedback(
            passed=passed,
            score=round(score, 4),
            issues=issues,
            # 通过时不给建议：下游若按"有建议即重做"处理，
            # 带着建议的通过会导致无限重做已经合格的镜头。
            suggestions=suggestions if not passed else [],
            model=self.settings.vlm_model,
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
            model=self.settings.vlm_model,
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
