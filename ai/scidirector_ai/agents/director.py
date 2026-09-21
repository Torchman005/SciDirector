"""导演智能体：把原始脚本拆解为结构化分镜表。

本模块的价值不只是「调一次模型」，更在于**对模型输出做严格的业务校验与修复**。
模型几乎不可能稳定满足「时长之和落在 ±10% 以内」这类硬约束，
如果在提示词里写一句"请遵守"就指望它遵守，线上一定会频繁翻车。
因此这里的策略是：**提示词约束 + 程序化兜底**，两者缺一不可。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..config import get_settings
from ..llm import LLMClient, LLMError, LLMParseError, Task
from ..logging import get_logger
from ..schemas import SceneTag, ScriptPlan, ShotSpec, StyleGuide, TAG_TO_ENGINE
from .base import Agent, render_prompt

logger = get_logger(__name__)

# 单个镜头的时长上下限。与提示词中的说明保持一致；
# 修改时必须同时改提示词，否则模型会输出被程序静默截断的值（难以察觉的偏差）。
MIN_SHOT_SEC = 2.0
MAX_SHOT_SEC = 25.0

# 允许的总时长偏差。10% 是经验值：更严会让模型很难满足，
# 更松会让「90 秒脚本生成出 70 秒视频」这种明显偏差被放过。
DURATION_TOLERANCE = 0.10


class _RawShot(BaseModel):
    """模型直出的分镜结构。

    与 :class:`ShotSpec` 分开的原因：模型输出**不应该**包含 shot_id / engine / code，
    用一个专门的窄结构做校验，可以把"模型越界输出"变成显式错误而不是被静默接受。
    """

    index: int = 0
    narration: str = ""
    visual_brief: str = ""
    tag: str = "AMBIENCE"
    duration_sec: float = 5.0
    keywords: list[str] = Field(default_factory=list)


class _RawPlan(BaseModel):
    """模型直出的完整规划结果。"""

    outline: str = ""
    shots: list[_RawShot] = Field(default_factory=list)


class DirectorAgent(Agent):
    """导演智能体。"""

    prompt_name = "director"

    def plan(
        self,
        *,
        job_id: str,
        raw_script: str,
        style_guide: StyleGuide,
        target_duration_sec: float,
        locale: str = "zh-CN",
    ) -> ScriptPlan:
        """把脚本拆解为分镜表。

        步骤：提示词 -> 模型 -> 结构校验 -> 业务修复 -> 派生字段。
        """
        settings = get_settings()
        user_prompt = render_prompt(
            "director_user",
            raw_script=raw_script,
            target_duration_sec=int(target_duration_sec),
            locale=locale,
            style_guide=style_guide,
        )

        self.log.info(
            "导演智能体开始拆解脚本",
            extra={
                "job_id": job_id,
                "script_chars": len(raw_script),
                "target_duration_sec": target_duration_sec,
            },
        )

        try:
            parsed = self.llm.chat_json(
                self.system_prompt(),
                user_prompt,
                _RawPlan,
                # 显式声明任务类型：mock 模式据此返回确定的结构，
                # 而不是从提示词里嗅探关键词（那曾导致返回错误结构并被静默解析）。
                task=Task.PLAN,
            )
        except (LLMParseError, LLMError) as exc:
            # 规划失败是**致命**的：没有分镜表，后面什么都做不了。
            # 因此这里直接抛出，由上层把任务标记为失败并提示用户（而不是产出空视频）。
            raise LLMError(f"导演智能体拆解脚本失败：{exc}") from exc

        assert isinstance(parsed, _RawPlan)  # 供类型检查器收敛

        shots = self._to_shot_specs(parsed.shots, job_id=job_id)
        shots = self._repair_durations(shots, target_duration_sec, job_id=job_id)

        plan = ScriptPlan(
            outline=parsed.outline.strip(),
            shots=shots,
            total_tokens=self.llm.usage.total_tokens,
        )
        self.log.info(
            "导演智能体拆解完成",
            extra={
                "job_id": job_id,
                "shot_count": len(plan.shots),
                "total_duration_sec": round(sum(s.duration_sec for s in plan.shots), 2),
                "target_duration_sec": target_duration_sec,
                "tag_distribution": _tag_distribution(plan.shots),
            },
        )
        return plan

    # ------------------------------------------------------------------
    # 校验与修复
    # ------------------------------------------------------------------

    def _to_shot_specs(self, raw_shots: list[_RawShot], *, job_id: str) -> list[ShotSpec]:
        """把模型输出转为领域模型，并补全派生字段（shot_id / engine）。"""
        if not raw_shots:
            raise LLMError("导演智能体返回了空的分镜表")

        shots: list[ShotSpec] = []
        for position, raw in enumerate(raw_shots):
            # 顺序以数组顺序为准，而不是模型给的 index：
            # 模型偶尔会给出重复或跳号的 index，按数组顺序重排更可靠。
            shot = ShotSpec(
                shot_id=f"{job_id}-s{position:03d}",
                index=position,
                narration=raw.narration.strip(),
                visual_brief=raw.visual_brief.strip(),
                tag=SceneTag.from_prompt(raw.tag),
                duration_sec=_clamp(raw.duration_sec, MIN_SHOT_SEC, MAX_SHOT_SEC),
                keywords=raw.keywords[:6],
            )
            # engine 由标签确定性推导（ShotSpec 的 model_validator 已处理），
            # 这里显式再取一次是为了让日志/调试一眼可见路由结果。
            shot.engine = TAG_TO_ENGINE[shot.tag]

            if not shot.visual_brief:
                # 视觉意图缺失会让编码智能体无从下手。给一个基于标签的兜底描述，
                # 而不是直接失败 —— 保留这一镜头比整片失败更有价值。
                shot.visual_brief = _fallback_visual_brief(shot)
                self.log.warning(
                    "分镜缺少视觉意图，已使用兜底描述",
                    extra={"job_id": job_id, "shot_id": shot.shot_id, "tag": shot.tag.value},
                )
            shots.append(shot)
        return shots

    def _repair_durations(
        self, shots: list[ShotSpec], target_duration_sec: float, *, job_id: str
    ) -> list[ShotSpec]:
        """把镜头时长之和修复到目标时长的 ±10% 以内。

        修复策略（按优先级）：
        1. 已经满足 -> 原样返回；
        2. 偏差在 3 倍容差以内 -> **等比例缩放**（保持镜头之间的相对节奏，
           这是最不破坏导演意图的做法）；
        3. 偏差过大（模型明显算错了）-> 按「平均分配」重算，但保留原时长的相对权重。
        """
        if not shots:
            return shots

        total = sum(s.duration_sec for s in shots)
        if total <= 0:
            avg = max(MIN_SHOT_SEC, target_duration_sec / len(shots))
            for s in shots:
                s.duration_sec = avg
            total = sum(s.duration_sec for s in shots)

        lower = target_duration_sec * (1 - DURATION_TOLERANCE)
        upper = target_duration_sec * (1 + DURATION_TOLERANCE)

        if lower <= total <= upper:
            return shots

        ratio = target_duration_sec / total
        # 限制单次缩放的幅度：比例过于极端时（例如模型只给了 3 秒却要求 90 秒），
        # 等比例缩放会把某些镜头压到 0.2 秒，那种视频是无法观看的。
        # 此时改用「保底 + 按权重分配」的策略。
        extreme = ratio > 3.0 or ratio < 0.33

        if extreme:
            self.log.warning(
                "分镜总时长与目标偏差过大，改用权重重分配",
                extra={"job_id": job_id, "total": round(total, 2), "ratio": round(ratio, 3)},
            )
            weights = [max(s.duration_sec, MIN_SHOT_SEC) for s in shots]
            weight_sum = sum(weights)
            for shot_i, weight in zip(shots, weights, strict=True):
                shot_i.duration_sec = _clamp(
                    target_duration_sec * weight / weight_sum, MIN_SHOT_SEC, MAX_SHOT_SEC
                )
        else:
            for shot_i in shots:
                shot_i.duration_sec = _clamp(
                    shot_i.duration_sec * ratio, MIN_SHOT_SEC, MAX_SHOT_SEC
                )

        repaired = sum(s.duration_sec for s in shots)
        self.log.info(
            "已修复分镜时长",
            extra={
                "job_id": job_id,
                "before_sec": round(total, 2),
                "after_sec": round(repaired, 2),
                "target_sec": target_duration_sec,
            },
        )
        return shots


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _clamp(value: float, low: float, high: float) -> float:
    """把数值夹到 [low, high]，并统一保留 2 位小数。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = low
    return round(max(low, min(high, v)), 2)


def _tag_distribution(shots: list[ShotSpec]) -> dict[str, int]:
    """统计标签分布。这个指标能直观暴露「模型把什么都打成 AMBIENCE」这类问题。"""
    dist: dict[str, int] = {}
    for s in shots:
        dist[s.tag.value] = dist.get(s.tag.value, 0) + 1
    return dist


def _fallback_visual_brief(shot: ShotSpec) -> str:
    """按标签生成兜底的视觉意图描述。

    兜底描述刻意写得保守（渐变、淡入），保证编码智能体一定做得出画面，
    而不是让它去猜一个不存在的信息。
    """
    return {
        SceneTag.MATH: "居中展示关键公式，逐项高亮推导步骤，末尾停留 1 秒。",
        SceneTag.DATA: "绘制居中的统计图表，元素从左到右生长，数值标签同步淡入。",
        SceneTag.CODE: "居中展示代码块，逐行打字并高亮当前行，末尾停留。",
        SceneTag.AMBIENCE: "背景渐变缓慢流动，标题文字淡入后保持，末尾淡出。",
    }[shot.tag]


__all__ = ["DirectorAgent", "DURATION_TOLERANCE", "MAX_SHOT_SEC", "MIN_SHOT_SEC", "LLMClient"]
