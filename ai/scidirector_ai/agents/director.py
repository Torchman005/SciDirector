"""导演智能体：把原始脚本拆解为结构化分镜表。

本模块的价值不只是「调一次模型」，更在于**对模型输出做严格的业务校验与修复**。
模型几乎不可能稳定满足「时长之和落在 ±10% 以内」这类硬约束，
如果在提示词里写一句"请遵守"就指望它遵守，线上一定会频繁翻车。
因此这里的策略是：**提示词约束 + 程序化兜底**，两者缺一不可。
"""

from __future__ import annotations

import math
import re

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

#: 单个镜头的时长上限。**超出的镜头会被拆开，而不是截断。**
#:
#: 为什么从 25 秒降到 15 秒：实测一条 **22.83 秒**的动效镜头，动画演到约 30%
#: 处就基本静止（相邻抽帧的"变化像素占比"一路是 `0.19% / 0.12% / 0.06%`），
#: 审查连续四轮（正确地）判它"动画停滞"，而每轮给的又都是量级不匹配的微调 ——
#: 一直烧到人工介入。**时长越长，越难让画面一直有变化**，这是结构问题，
#: 不是提示词能救的（把要求写进提示词后实测只能从"30% 处静止"改善到"70% 处"）。
MAX_SHOT_SEC = 15.0

#: 模型偶尔会给出离谱的时长（例如把整段脚本都算进一个镜头）。
#: 拆镜头的前置钳制：超过这个值先夹住，避免一个 600 秒的镜头被拆成 40 段。
_ABSURD_SHOT_SEC = MAX_SHOT_SEC * 4

#: 旁白的句子结束标点。拆镜头时按它们切分，**不把一个句子劈成两半** ——
#: 半句话既会让配音听起来断裂，也会让该段的画面意图变得没法理解。
_SENTENCE_END = "。！？；!?;\n"

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
    beats: list[str] = Field(default_factory=list)


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
        # 顺序是刻意的：先拆超长镜头，再对齐总时长，最后**再拆一次**。
        #
        # 为什么最后还要拆：`_repair_durations` 为了让总和落进 ±10% 会**等比放大**，
        # 放大后可能又有镜头超过上限。这一步不能省 —— 而它之所以安全，
        # 是因为 `_split_long_shots` **保持总时长不变**（各段之和等于原时长），
        # 所以拆完不需要再对齐一次。
        shots = self._split_long_shots(shots, job_id=job_id)
        shots = self._repair_durations(shots, target_duration_sec, job_id=job_id)
        shots = self._split_long_shots(shots, job_id=job_id)

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
                # 只兜**下限**：上限交给 `_split_long_shots` 处理。
                # 这里若按 MAX_SHOT_SEC 截断，22.8 秒会先变成 15 秒，
                # 拆分逻辑就再也看不到"这个镜头过长"这件事了 ——
                # 结果是把 7.8 秒的旁白悄悄裁掉，而且没有任何日志。
                duration_sec=max(float(raw.duration_sec), MIN_SHOT_SEC),
                keywords=raw.keywords[:6],
                beats=[str(b).strip() for b in raw.beats[:8] if str(b).strip()],
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
                shot_i.duration_sec = _clamp_lower(
                    target_duration_sec * weight / weight_sum, MIN_SHOT_SEC
                )
        else:
            for shot_i in shots:
                shot_i.duration_sec = _clamp_lower(
                    shot_i.duration_sec * ratio, MIN_SHOT_SEC
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

    def _split_long_shots(self, shots: list[ShotSpec], *, job_id: str) -> list[ShotSpec]:
        """把超过 :data:`MAX_SHOT_SEC` 的镜头**拆成多个**，而不是截断。

        为什么必须拆、不能截断：`duration_sec` 决定这一镜头的**画面时长**，
        而 Go 侧合成时会把该镜头的配音**对齐到画面时长**（不足补静音、超长裁掉）。
        把 22.8 秒截成 15 秒会**裁掉 7.8 秒的旁白**；而不截断则会让编码端去填
        它填不满的时间 —— 实测那条 22.83 秒镜头的后半段就是全静止的。

        拆的规则：

        * 旁白按**句子边界**切（半句话会让配音听起来断裂）；
        * 各段时长按**字数比例**分配 —— 这样每段的音画仍然对得上；
        * 视觉意图附上"这是第几段"的说明，避免编码端把同一个画面画 N 遍。

        没有旁白的镜头（氛围/标题）没有可切的句子，退化为**等分时长**。
        """
        if not shots:
            return shots

        out: list[ShotSpec] = []
        for shot in shots:
            parts = max(1, math.ceil(shot.duration_sec / MAX_SHOT_SEC - 1e-6))
            if parts <= 1:
                out.append(shot)
                continue

            sentences = _split_sentences(shot.narration)
            groups = _group_sentences(sentences, parts)
            if not groups:
                # 完全没有旁白（氛围/标题镜头）：没有可切的句子，等分时长。
                groups = [""] * parts
            elif len(groups) < parts:
                # 句子不够：**不要**用空串补齐（那会产出没有旁白的镜头）。
                # 少拆几段，接受镜头略长，并把这件事记下来 ——
                # 它说明"一句话撑了二十多秒"，那是导演侧的问题而不是拆分能解决的。
                self.log.warning(
                    "镜头过长但旁白句子不足以拆分，保留较长的镜头",
                    extra={
                        "job_id": job_id,
                        "shot_id": shot.shot_id,
                        "duration_sec": round(shot.duration_sec, 2),
                        "want_parts": parts,
                        "sentence_count": len(sentences),
                        "actual_parts": len(groups),
                    },
                )
                parts = len(groups)

            weights = [max(len(g), 1) for g in groups]
            weight_sum = sum(weights)
            beat_groups = _distribute_beats(shot.beats, weights)
            visual_steps = _split_visual_steps(shot.visual_brief)
            visual_groups = _distribute_beats(visual_steps, weights)
            for position, (group, weight) in enumerate(zip(groups, weights, strict=True)):
                segment = shot.model_copy(deep=True)
                segment.narration = group
                segment.duration_sec = round(shot.duration_sec * weight / weight_sum, 2)
                segment.beats = beat_groups[position]
                assigned_visual = "".join(visual_groups[position]).strip()
                if not assigned_visual:
                    assigned_visual = f"承接前一镜头，呈现本段旁白：{group}" if group else shot.visual_brief
                if beat_groups[position]:
                    assigned_visual += "\n本段画面节拍：" + "；".join(beat_groups[position])
                segment.visual_brief = _continuation_brief(
                    assigned_visual, position, parts
                )
                out.append(segment)

            self.log.info(
                "镜头过长，已拆分为多段",
                extra={
                    "job_id": job_id,
                    "shot_id": shot.shot_id,
                    "duration_sec": round(shot.duration_sec, 2),
                    "parts": parts,
                    "max_shot_sec": MAX_SHOT_SEC,
                    "part_durations": [
                        round(shot.duration_sec * w / weight_sum, 2) for w in weights
                    ],
                },
            )

        if len(out) == len(shots):
            return shots

        # 拆分后必须重新编号：index 与 shot_id 都要连续，
        # 否则下游按 index 定位镜头的地方（写回、事件、界面排序）会错位。
        for position, segment in enumerate(out):
            segment.index = position
            segment.shot_id = f"{job_id}-s{position:03d}"
        return out


def _distribute_beats(beats: list[str], weights: list[int]) -> list[list[str]]:
    """Keep ordered beats on one segment each, weighted by narration length."""
    if not weights:
        return []
    count = len(beats)
    total = sum(weights)
    boundaries = [0]
    cumulative = 0
    for weight in weights[:-1]:
        cumulative += weight
        boundaries.append(round(count * cumulative / total))
    boundaries.append(count)
    return [beats[boundaries[i]:boundaries[i + 1]] for i in range(len(weights))]


def _split_visual_steps(brief: str) -> list[str]:
    """Preserve ordered visual actions so each split shot gets a distinct scope."""
    return [part for part in re.split(r"(?<=[，,；;。])", brief) if part.strip()]


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _split_sentences(text: str) -> list[str]:
    """把旁白切成句子（保留标点）。"""
    out: list[str] = []
    buf: list[str] = []
    for ch in text:
        buf.append(ch)
        if ch in _SENTENCE_END:
            sentence = "".join(buf).strip()
            if sentence:
                out.append(sentence)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _group_sentences(sentences: list[str], parts: int) -> list[str]:
    """把句子按字符数尽量均分成 ``parts`` 组。

    按**字符数**而不是句子条数分组：中文里一句话的长短差别很大
    （"但是。" 与 "在自注意力机制中，每个词都要和其余所有词计算相关性。"），
    按条数分会得到一段很长的配音配一段很短的画面。

    **组数不会超过句子数**：句子不够时宁可少分几组（镜头略长），
    也不要产出"没有旁白的镜头" —— 那种镜头的画面没有任何解说支撑，
    只会变成一个更小的静止画面问题。

    切点选择：在"累计字符数最接近等分点"的句子边界上切，
    并且**给后面每组至少留一句**（否则会切出空组）。
    """
    if not sentences:
        return []
    count = len(sentences)
    parts = max(1, min(parts, count))
    if parts == 1:
        return ["".join(sentences)]

    cumulative: list[int] = []
    acc = 0
    for sentence in sentences:
        acc += len(sentence)
        cumulative.append(acc)
    total = acc

    groups: list[str] = []
    start = 0
    for k in range(1, parts):
        # 切点 i 表示 sentences[start:i] 归本组；上界要保证后面每组非空。
        upper = count - (parts - k)
        target = total * k / parts
        best_i, best_err = start + 1, None
        for i in range(start + 1, upper + 1):
            err = abs(cumulative[i - 1] - target)
            if best_err is None or err < best_err:
                best_i, best_err = i, err
        groups.append("".join(sentences[start:best_i]))
        start = best_i
    groups.append("".join(sentences[start:]))
    return [g for g in groups if g]


def _continuation_brief(brief: str, index: int, parts: int) -> str:
    """给拆分出来的段落加上"这是第几段"的说明。

    不加说明的话，编码端拿到的是同一段视觉意图的 N 份拷贝，
    很可能把同一个画面画 N 遍 —— 那恰恰是"每段都要有变化"的反面。
    同时这也是**给审核员看的**：一眼能看出这几个镜头是同段内容拆开的。
    """
    if parts <= 1:
        return brief
    if index == 0:
        hint = "（同一段内容的第 1 段，先把场景与主体建立起来，不要急着收束）"
    elif index == parts - 1:
        hint = f"（同一段内容的最后一段（第 {parts} 段），承接上一镜头继续推进并收束）"
    else:
        hint = f"（同一段内容的第 {index + 1}/{parts} 段，承接上一镜头继续推进）"
    return f"{brief}\n{hint}" if brief else hint


def _clamp(value: float, low: float, high: float) -> float:
    """把数值夹到 [low, high]，并统一保留 2 位小数。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = low
    return round(max(low, min(high, v)), 2)


def _clamp_lower(value: float, low: float) -> float:
    """只兜下限、**不设上限**，保留 2 位小数。

    为什么时长修复不能用带上限的 clamp：上限一旦生效，被夹掉的那部分时长
    **就此消失**（没有任何日志），而总和必须落在目标 ±10% —— 实测目标 60 秒
    的任务因此只产出 **47.42 秒**（放大后被夹在 15.0 秒的镜头不再触发拆分，
    因为 `ceil(15/15 - ε) = 1`）。

    上限改由 :meth:`DirectorAgent._split_long_shots` 负责：
    **拆分是守恒的**（各段之和等于原时长），而 clamp 是丢失的。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = low
    return round(max(low, v), 2)


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
        # 环境镜头现在也走 HTML 动效：动态背景 + 一行标题（不再是一张固定渐变）。
        SceneTag.AMBIENCE: "动态背景缓慢流动（光晕/呼吸/粒子），标题文字淡入后保持，末尾淡出。",
        SceneTag.MOTION: "居中绘制一个界面或图形，元素依次进入并完成一个明确动作，末尾停留。",
    }[shot.tag]


__all__ = ["DirectorAgent", "DURATION_TOLERANCE", "MAX_SHOT_SEC", "MIN_SHOT_SEC", "LLMClient"]
