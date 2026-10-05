"""LangGraph 节点实现 —— 多智能体流水线的执行单元。

图的形状（见 docs/DESIGN.md §5.2）：

    plan ─▶ code ─▶ render ─▶ critique ─┬─(通过)─▶ advance ─┬─(还有镜头)─▶ code
      ▲        ▲                         │                   └─(全部完成)─▶ END
      │        │                         └─(不通过)─▶ revise ─┬─(额度未尽)─▶ code
      │        └──────────────────────────────────────────────┘
      │                                                  └─(熔断)─▶ advance

每个节点返回**部分状态**，LangGraph 负责合并。
两个容易踩的坑：

1. ``events`` 使用 ``operator.add`` reducer，返回列表即追加；
   串行图的字典整体替换；并行父图按镜头合并。节奏报告用 ``None`` 清除旧结论。
2. ``route_hint`` 是控制流的唯一依据（见 state.py 的说明）。
"""

from __future__ import annotations

import functools
import hashlib
from difflib import SequenceMatcher

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agents.coder import CoderAgent
from ..agents.critic import CriticAgent
from ..agents.director import DirectorAgent
from ..agents.base import render_prompt
from ..config import Settings, browser_ready
from ..llm import LLMClient, LLMError
from ..logging import get_logger
from ..media import (
    FrameSample,
    MediaToolError,
    MotionReport,
    analyze_motion,
    extract_frames_with_times,
)
from ..pbconv import shots_payload_json
from ..review_tasks import (format_repairs, merge_repairs, active_repairs,
                            update_repair_progress, REPAIR_STRATEGIES)
from ..repair_evidence import precheck_repairs, skip_full_review
from ..renderer import LLM_ENGINES, Renderer, RendererError, RenderRequest, build_renderer
from ..sandbox.runner import SandboxRunner
from ..tts.base import synthesize_with_retry, write_marks_sidecar
from ..schemas import CriticFeedback, FeedbackSource, RenderArtifact, RenderEngine, ShotSpec, StyleGuide
from .state import (
    NODE_ADVANCE,
    NODE_CODE,
    NODE_CRITIQUE,
    NODE_PLAN,
    NODE_RENDER,
    NODE_REVISE,
    PipelineState,
    current_shot,
    make_event,
    progress_ratio,
    shot_attempt,
)

logger = get_logger(__name__)


def _motion_feedback(report: MotionReport, shot: ShotSpec) -> str:
    summary = report.summary()
    if "卡在" in shot.visual_brief or "定格" in shot.visual_brief:
        summary += "\n视觉意图包含停留或定格；先核对静止时段是否符合计划，不要仅凭此项打回。"
    if not shot.beats or shot.duration_sec <= 0:
        return summary
    beat_count = len(shot.beats)
    affected = sorted({
        min(beat_count - 1, int(((start + end) / 2) / shot.duration_sec * beat_count))
        for start, end, _ in report.static_spans
    })
    details = "；".join(f"第 {i + 1} 段「{shot.beats[i]}」" for i in affected)
    return f"{summary}\n对应画面节拍：{details}。"


def _frame_signatures(paths: list[str]) -> list[str]:
    """Store image content, not paths: the next render overwrites the same frame names."""
    try:
        return [hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in paths]
    except OSError:
        return []


def _same_unresolved_feedback(previous: CriticFeedback | None, current: CriticFeedback) -> bool:
    if previous is None or previous.passed or current.passed:
        return False
    def normalized(feedback: CriticFeedback) -> tuple[str, ...]:
        return tuple(" ".join(text.lower().split()) for text in [
            *feedback.issues, *feedback.suggestions,
        ] if text.strip())
    signature = normalized(current)
    prior = normalized(previous)
    return (
        bool(signature) and bool(prior)
        and SequenceMatcher(None, "\n".join(signature), "\n".join(prior)).ratio() >= 0.9
        and current.score <= previous.score + 0.02
    )

# ---------------------------------------------------------------------------
# 控制流提示（route_hint）
# ---------------------------------------------------------------------------
# 用常量而不是裸字符串：拼错一个字母的后果是条件边走到默认分支，
# 表现为"流水线莫名其妙地结束了"，极难排查。
HINT_RENDER = "render"      # code 成功 -> 去渲染
HINT_RETRY = "retry"        # 失败 -> 去 revise（携带反馈）
HINT_CRITIQUE = "critique"  # 渲染成功 -> 去审查
HINT_OK = "ok"              # 审查通过 -> 去 advance
HINT_HUMAN = "human"        # 熔断 -> 转人工，然后继续下一镜头
HINT_NEXT = "next"          # advance -> 下一个镜头
HINT_DONE = "done"          # 全部完成


class PipelineError(RuntimeError):
    """流水线级别的致命错误（无法通过重试解决）。"""


#: 「无进展熔断」的分数下限。
#:
#: 得分低于它就认为"离及格还很远"，配合"没有进步"才提前转人工。
#: 取 0.5 的理由：实测被反复打回的镜头稳定落在 0.30~0.45 区间，而慢热但能翻盘的
#: 镜头通常在 0.5~0.7 波动 —— 这条线正好把两者分开。
_HOPELESS_SCORE = 0.5


@dataclass
class PipelineDeps:
    """节点所需的全部依赖。

    用依赖容器而不是让每个节点自己去 new 对象：渲染器是**有状态**的
    （HtmlRenderer 需要复用浏览器配置、Runner 持有并发闸门），
    重复构造会带来资源浪费与行为不一致。
    """

    settings: Settings
    llm: LLMClient
    director: DirectorAgent
    coder: CoderAgent
    critic: CriticAgent
    runner: SandboxRunner
    #: TTS 服务商。为 None 表示不合成配音 —— 缺省必须是一条能跑通的路径，
    #: 没配 TTS 不该让流水线失败，也不该产出任何额外文件。
    tts: object | None = None
    _renderers: dict[str, Renderer] = field(default_factory=dict, repr=False)

    def renderer(self, engine: str) -> Renderer:
        """按引擎取渲染器（带缓存）。"""
        cached = self._renderers.get(engine)
        if cached is None:
            cached = build_renderer(engine, self.settings)
            self._renderers[engine] = cached
        return cached



def traced_node(name: str) -> Any:
    """给图节点套一层 span。

    用装饰器而不是在每个节点里手写 `with tracer.start_as_current_span(...)`：
    节点有 7 个、将来还会加，手写必然有人漏 —— 漏掉的表现是「链路里少一段」，
    不会报错，只会让人以为那段时间没花在 Python 侧。

    span 名字就是节点名（plan/code/render/critique/revise/advance），
    与事件流里的 `node` 字段**同名**：这样「事件里的 node」与
    「Tempo 里的 span」能直接对上，不必再维护一份映射表。
    """

    def wrap(fn: Any) -> Any:
        @functools.wraps(fn)
        def inner(self: Any, state: Any) -> Any:
            from ..obs import tracer

            shot_index = state.get("current_index", 0)
            with tracer().start_as_current_span(name) as span:
                span.set_attribute("scidirector.node", name)
                span.set_attribute("scidirector.job_id", str(state.get("job_id", "")))
                span.set_attribute("scidirector.shot_index", int(shot_index))
                return fn(self, state)

        return inner

    return wrap


class PipelineNodes:
    """流水线节点集合。每个公开方法都是一个 LangGraph 节点。"""

    def __init__(self, deps: PipelineDeps) -> None:
        self.deps = deps

    # ==================================================================
    # plan：导演智能体拆解脚本
    # ==================================================================

    @traced_node("plan")
    def plan(self, state: PipelineState) -> dict[str, Any]:
        """脚本 -> 结构化分镜表。

        这是**唯一不可重试**的节点：没有分镜表，后面什么都做不了。
        因此失败直接抛异常，由上层把任务标记为失败并提示用户 ——
        而不是产出"零个镜头"的空任务，让前端一直转圈。
        """
        job_id = state.get("job_id", "")
        style = state.get("style_guide") or StyleGuide()
        started = time.monotonic()

        try:
            plan = self.deps.director.plan(
                job_id=job_id,
                raw_script=state.get("raw_script", ""),
                style_guide=style,
                target_duration_sec=float(state.get("target_duration_sec", 90.0)),
                locale=state.get("locale", "zh-CN"),
            )
        except LLMError as exc:
            raise PipelineError(f"导演智能体拆解失败：{exc}") from exc

        shots = plan.shots
        if not shots:
            raise PipelineError("导演智能体未产出任何分镜，无法继续")

        # attempts 必须**整体重建**：它会在 code 节点被逐镜头累加。
        attempts = {s.shot_id: 0 for s in shots}

        snapshot: PipelineState = {**state, "shots": shots}  # type: ignore[assignment]
        event = make_event(
            snapshot,
            node=NODE_PLAN,
            message=(
                f"导演完成拆解：{len(shots)} 个分镜，总时长 "
                f"{sum(s.duration_sec for s in shots):.1f}s"
            ),
            status="PENDING",
            # 分镜表快照：Go 侧据此把镜头落库（跨语言约定见 docs/API.md）。
            payload_json=shots_payload_json(shots, plan.outline),
        )

        logger.info(
            "plan 完成",
            extra={
                "job_id": job_id,
                "shots": len(shots),
                "elapsed_sec": round(time.monotonic() - started, 2),
                "tags": _tag_distribution(shots),
            },
        )
        return {
            "shots": shots,
            "outline": plan.outline,
            "cursor": 0,
            "attempts": attempts,
            "artifacts": {},
            "feedback": {},
            "frame_signatures": {},
            "revision_stagnation": {},
            "human_feedback": dict(state.get("human_feedback") or {}),
            "current_code": "",
            "current_language": "",
            "render_error": "",
            "route_hint": HINT_NEXT,
            "events": [event],
            "total_tokens": self.deps.llm.usage.total_tokens,
        }

    # ==================================================================
    # code：编码智能体生成渲染代码
    # ==================================================================

    def _engine_or_fallback(
        self, shot: ShotSpec, state: PipelineState
    ) -> tuple[ShotSpec, dict[str, Any] | None]:
        """引擎不可用时**如实降级**，而不是让镜头直接失败。

        环境镜头（``AMBIENCE``）现在也走 HTML 动画，而 HTML 需要 headless 浏览器。
        浏览器可能没装 —— 而"环境镜头永远画得出来"（只要 ffmpeg）正是它作为
        **兜底镜头**的全部价值，不能因为换了默认实现就丢掉。

        所以这里退回 ffmpeg 渐变，并留一条事件说明。
        "静默降级"和"如实降级"的区别就在这里：前者事后无法解释
        "这个镜头为什么突然变简单了"。
        """
        if shot.engine is not RenderEngine.MOTION:
            return shot, None

        ok, reason = browser_ready()
        if ok:
            return shot, None

        logger.warning(
            "headless 浏览器不可用，环境镜头降级为 ffmpeg 渐变",
            extra={"shot_id": shot.shot_id, "reason": reason[:200]},
        )
        degraded = shot.model_copy(update={"engine": RenderEngine.STOCK})
        return degraded, make_event(
            state, node=NODE_CODE, shot=shot,
            message=f"headless 浏览器不可用（{reason[:80]}），本镜头降级为程序化渐变",
            status="GENERATING",
        )

    @traced_node("code")
    def code(self, state: PipelineState) -> dict[str, Any]:
        """为当前镜头生成渲染代码。

        包含一条**便宜的快速失败路径**：静态安全检查不通过时直接返回 retry，
        跳过渲染。一次渲染是数十秒，而静态检查是毫秒级 ——
        在这里拦住一个 `import os` 就等于省下一整轮渲染。
        """
        shot = current_shot(state)
        if shot is None:
            return {"route_hint": HINT_DONE, "finished": True}

        # 引擎可能不可用（环境镜头现在走 HTML，而 HTML 需要 headless 浏览器）。
        # 必须**在调用编码智能体之前**决定：换引擎会换提示词，
        # 而且降级回 stock 时标题是由 `_programmatic_ambient` 从画外音截出来的。
        shot, engine_event = self._engine_or_fallback(shot, state)

        style = state.get("style_guide") or StyleGuide()
        attempt = shot_attempt(state, shot.shot_id) + 1
        feedback_text = _collect_feedback(state, shot.shot_id)

        started = time.monotonic()
        try:
            result = self.deps.coder.generate(
                shot=shot,
                style_guide=style,
                attempt=attempt,
                feedback_text=feedback_text,
                previous_code=state.get("current_code", ""),
            )
        except LLMError as exc:
            # 模型调用失败属于可重试错误：交给 revise 决定是再试还是转人工。
            logger.warning(
                "code 节点生成失败",
                extra={"shot_id": shot.shot_id, "error": str(exc)[:300]},
            )
            return {
                "attempts": {**(state.get("attempts") or {}), shot.shot_id: attempt},
                "render_error": str(exc),
                "route_hint": HINT_RETRY,
                "events": [
                    make_event(
                        state, node=NODE_CODE, message=f"代码生成失败：{exc}",
                        status="RETRYING", shot=shot, attempt=attempt,
                        error=str(exc)[:500],
                    )
                ],
            }

        attempts = dict(state.get("attempts") or {})
        attempts[shot.shot_id] = attempt

        prior_feedback = (state.get("feedback") or {}).get(shot.shot_id)
        prior_error = state.get("render_error", "")
        if (
            attempt > 1
            and result.policy_ok
            and state.get("current_code", "").strip()
            and result.code.strip() == state.get("current_code", "").strip()
            and isinstance(prior_feedback, CriticFeedback)
            and not prior_feedback.passed
            and prior_feedback.source is FeedbackSource.VLM
            and (not prior_error or prior_error.startswith(("新源码与上一轮", "新旧审查抽帧", "本轮修复任务")))
        ):
            reason = "新源码与上一轮逐字相同，画面不会改善；请重构反馈指出的画面阶段或元素"
            return {
                "attempts": attempts,
                "render_error": reason,
                "route_hint": HINT_RETRY,
                "events": [make_event(
                    state, node=NODE_CODE, shot=shot, attempt=attempt,
                    message=reason + "，已跳过无效渲染与审查", status="RETRYING",
                )],
            }

        ok = result.policy_ok
        engine_name = shot.engine.value if shot.engine else "?"
        message = (
            f"已生成 {engine_name} 代码（{len(result.code)} 字符，第 {attempt} 次尝试）"
            if ok
            else f"生成的代码未通过安全/质量检查：{result.policy_summary[:200]}"
        )

        # 把镜头写回列表。**无条件写回**（而不是只在有 overlay_text 时）：
        # 引擎可能刚被 `_engine_or_fallback` 改过，不持久化的话渲染节点读到的
        # 仍是旧引擎，降级就白做了。写回本身是幂等的。
        shots = list(state.get("shots") or [])
        updated = shot
        if result.overlay_text:
            updated = updated.model_copy(update={
                "meta": {**updated.meta, "overlay_text": result.overlay_text}
            })
        if 0 <= shot.index < len(shots):
            shots[shot.index] = updated

        events: list[dict[str, Any]] = []
        if engine_event is not None:
            # 降级说明放在生成事件**之前**，读事件流的人先看到"为什么换了引擎"。
            events.append(engine_event)
        events.append(
            make_event(
                state, node=NODE_CODE, message=message, status="GENERATING",
                shot=shot, attempt=attempt, error="" if ok else result.policy_summary[:500],
                # 跨语言补丁：把生成的代码同步给 Go，
                # 使 HITL 重做时能带着上一版代码重写。
                payload_json=_code_patch(shot.shot_id, result.code, result.artifact.language,
                                         generation_quality={"checked": result.quality.checked,
                                             "issues": result.quality.issues, "warnings": result.quality.warnings,
                                             "reason": result.quality.reason, "llm_attempts": result.llm_attempts}),
            )
        )

        logger.info(
            "code 完成",
            extra={
                "shot_id": shot.shot_id,
                "engine": engine_name,
                "attempt": attempt,
                "policy_ok": ok,
                "skipped_llm": result.skipped_llm,
                "examples": len(result.examples_used),
                "elapsed_sec": round(time.monotonic() - started, 2),
            },
        )
        return {
            "shots": shots,
            "current_code": result.code,
            "current_language": result.artifact.language,
            "attempts": attempts,
            "render_error": "" if ok else result.policy_summary,
            "route_hint": HINT_RENDER if ok else HINT_RETRY,
            "events": events,
            "total_tokens": self.deps.llm.usage.total_tokens,
        }

    # ==================================================================
    # render：沙盒渲染
    # ==================================================================

    @traced_node("render")
    def render(self, state: PipelineState) -> dict[str, Any]:
        """在沙盒中执行代码，产出视频片段并抽帧。

        抽帧放在这里而不是审查节点：只有渲染现场才知道真实时长与产物路径，
        让调用方自己再抽一次会出现"用了错误时长导致抽到黑帧"的问题。
        """
        shot = current_shot(state)
        if shot is None:
            return {"route_hint": HINT_DONE, "finished": True}

        engine = shot.engine.value if shot.engine else "stock"
        style = state.get("style_guide") or StyleGuide()
        job_id = state.get("job_id", "")
        attempt = shot_attempt(state, shot.shot_id)
        out_dir = (Path(self.deps.settings.sandbox_work_dir) / job_id / f"shot_{shot.index:03d}"
                   / f"attempt_{attempt:02d}")

        request = RenderRequest(
            shot_id=shot.shot_id,
            code=state.get("current_code", ""),
            output_dir=out_dir,
            duration_sec=shot.duration_sec,
            width=self.deps.settings.render_width,
            height=self.deps.settings.render_height,
            fps=self.deps.settings.render_fps,
            draft=False,
            overlay_text=shot.meta.get("overlay_text", ""),
            primary_color=style.primary_color,
            background_color=style.background_color,
        )

        started = time.monotonic()
        try:
            renderer = self.deps.renderer(engine)
            result = renderer.render(request, self.deps.runner)
        except RendererError as exc:
            elapsed = time.monotonic() - started
            hint = HINT_RETRY if exc.retryable else HINT_HUMAN
            detail = (exc.detail or "")[:800]
            logger.warning(
                "render 失败",
                extra={
                    "shot_id": shot.shot_id, "engine": engine, "attempt": attempt,
                    "retryable": exc.retryable, "elapsed_sec": round(elapsed, 2),
                    "error": str(exc)[:300],
                },
            )
            # **状态语义必须和路由一致**：不可重试的失败会由 route_after_render
            # 直接送到 advance（绕过 revise），因此这里必须自己发
            # AWAITING_HUMAN —— 否则整个事件流里只有 FAILED，
            # Go 侧会把它当成"技术失败"，而不是"这个镜头需要人工介入"。
            if exc.retryable:
                status = "RETRYING"
                message = f"渲染失败（{engine}）：{exc}"
            else:
                status = "AWAITING_HUMAN"
                message = f"渲染失败且无法重试（{engine}）：{exc} → 转人工处理"
            return {
                "render_error": f"{exc}\n{detail}".strip(),
                "route_hint": hint,
                "events": [
                    make_event(
                        state, node=NODE_RENDER, message=message, status=status,
                        shot=shot, attempt=attempt, error=f"{exc}\n{detail}"[:1500],
                    )
                ],
            }

        # 抽帧：审查节点依赖它。抽帧失败不应让整个镜头失败 ——
        # 交给审查节点去降级（它会因为"无帧可用"而转人工）。
        #
        # 用 `extract_frames_with_times`：节奏检查要回答"第几秒到第几秒没变化"，
        # 必须拿到每帧的**真实时间点**。时间点由 media 侧统一计算，
        # 这里不另算一遍 —— 两处各算一遍会让反馈里的秒数与实际抽帧对不上。
        frames: list[str] = []
        samples: list[FrameSample] = []
        try:
            samples = extract_frames_with_times(
                result.video_path,
                out_dir / "frames",
                self.deps.runner,
                count=self.deps.settings.critic_frame_samples,
                duration_sec=result.duration_sec,
                # 显式传宽度：审查提示词要按**同一个值**告诉 VLM 缩略图的换算比例，
                # 两处各自取默认值就会悄悄分叉（见 config.critic_frame_width）。
                width=self.deps.settings.critic_frame_width,
            )
            frames = [s.path for s in samples]
        except MediaToolError as exc:
            logger.warning(
                "抽帧失败，审查将降级",
                extra={"shot_id": shot.shot_id, "error": str(exc)[:200]},
            )

        signatures = _frame_signatures(frames)
        previous_signatures = (state.get("frame_signatures") or {}).get(shot.shot_id)
        previous_feedback = (state.get("feedback") or {}).get(shot.shot_id)
        if (
            attempt > 1
            and signatures
            and signatures == previous_signatures
            and isinstance(previous_feedback, CriticFeedback)
            and not previous_feedback.passed
            and previous_feedback.source is FeedbackSource.VLM
            and not previous_feedback.repair_tasks
        ):
            reason = (
                "新旧审查抽帧逐张完全相同，源码改动没有产生可见效果；"
                "请重做反馈所指阶段的画面结构，不要只微调参数"
            )
            return {
                "render_error": reason,
                "route_hint": HINT_RETRY,
                "events": [make_event(
                    state, node=NODE_RENDER, shot=shot, attempt=attempt,
                    message=reason + "，已跳过重复视觉审查", status="RETRYING",
                )],
            }

        evidence_artifact = RenderArtifact(video_path=str(result.video_path), duration_sec=result.duration_sec)
        prechecks = precheck_repairs(
            previous_feedback, (state.get("artifacts") or {}).get(shot.shot_id), evidence_artifact,
            (state.get("review_samples") or {}).get(shot.shot_id, []), samples,
            out_dir / "repair_evidence", self.deps.runner,
        )
        if skip_full_review(prechecks):
            reason = "本轮修复任务对应的可读性证据帧没有可见变化，需重构指定对象"
            return {
                "repair_prechecks": {**(state.get("repair_prechecks") or {}), shot.shot_id: prechecks},
                "render_error": reason, "route_hint": HINT_RETRY,
                "events": [make_event(state, node=NODE_RENDER, shot=shot, attempt=attempt,
                                      message=reason + "，跳过无效完整审核", status="RETRYING",
                                      payload_json=json.dumps({"repair_prechecks": prechecks}, ensure_ascii=False))],
            }

        # 节奏检查：把"画面有没有贯穿整段时长都在变"**算出来**。
        #
        # 为什么要算而不是只靠 VLM 看：实测一条 22.83 秒的镜头动画演到约 30% 处
        # 就静止，审查连续四轮都（正确地）判"动画停滞"，但每轮给的都是
        # "把打字 run_time 延长到 3 秒"这类**量级不匹配**的微调 ——
        # 编码端照做了，画面依然静止，于是改来改去画面不变、结论一字不差。
        # 算出来的结论能带**具体时段**，这才是编码端能执行的反馈。
        #
        # **单独抽一次帧**，不复用审查那批：
        # 审查的张数受 token 成本约束（每张都要传给 VLM），而这里是纯本地计算。
        # 复用审查那批会漏检 —— 实测同一条镜头，4 张（间隔 5.7s）测出
        # `11.15 / 2.22 / 1.04 / 1.15` 全部高于阈值，而 10 张才看得出
        # 中间那三段 `0.19 / 0.12 / 0.06` 的真静止：5.7 秒的窗口里，
        # 缓慢漂移也能累积出 1% 的变化，把静止抹平了。
        motion_report = MotionReport(ratios=[], static_spans=[], min_change_ratio=0.0)
        try:
            pacing_samples = extract_frames_with_times(
                result.video_path,
                out_dir / "frames_pacing",
                self.deps.runner,
                count=self.deps.settings.pacing_frame_samples,
                duration_sec=result.duration_sec,
                width=self.deps.settings.pacing_frame_width,
            )
            motion_report = analyze_motion(
                pacing_samples,
                min_change_ratio=self.deps.settings.pacing_min_change_ratio,
                min_span_sec=self.deps.settings.pacing_min_span_sec,
            )
        except MediaToolError as exc:
            # 抽帧失败只降级：节奏检查是**附加**信号，不能因为没有它就不出片。
            logger.warning(
                "节奏检查抽帧失败（不影响出片）",
                extra={"shot_id": shot.shot_id, "error": str(exc)[:200]},
            )
        except Exception as exc:  # noqa: BLE001 - 分析失败同样不该影响出片
            logger.warning(
                "节奏检查失败（不影响出片）",
                extra={"shot_id": shot.shot_id, "error": str(exc)[:200]},
            )

        artifact = RenderArtifact(
            artifact_id=f"{job_id}-{shot.shot_id}-{attempt}-{uuid.uuid4().hex[:6]}",
            shot_id=shot.shot_id,
            video_path=result.video_path,
            duration_sec=result.duration_sec,
            width=result.width,
            height=result.height,
            fps=int(result.fps) or self.deps.settings.render_fps,
            attempt=attempt,
            engine=result.engine,
            frame_samples=frames,
            rendered_at_unix_ms=int(time.time() * 1000),
            render_cost_sec=round(result.render_cost_sec, 3),
        )

        # 配音：与抽帧同样的降级姿势 —— 失败只降级，不让镜头失败。
        #
        # 为什么不让它失败：画面才是主体。一个没有旁白的镜头仍是可用产物，
        # 而「因为 TTS 抖动就丢掉整个镜头」是把外部依赖的问题升级成内容事故。
        # 但**必须留痕**：不配音是可见的质量差异（成片没声音），
        # 静默降级会让人以为「TTS 接好了但没生效」，方向完全错。
        audio_path, tts_error = self._synthesize_narration(shot, out_dir)
        artifact.audio_path = audio_path

        artifacts = dict(state.get("artifacts") or {})
        artifacts[shot.shot_id] = artifact
        elapsed = time.monotonic() - started

        logger.info(
            "render 完成",
            extra={
                "shot_id": shot.shot_id, "engine": result.engine, "attempt": attempt,
                "duration_sec": round(result.duration_sec, 2), "frames": len(frames),
                "elapsed_sec": round(elapsed, 2),
            },
        )
        events = [
            make_event(
                state, node=NODE_RENDER,
                message=(
                    f"渲染完成：{result.duration_sec:.1f}s / "
                    f"{result.width}x{result.height} / {len(frames)} 帧抽帧"
                ),
                status="CRITIQUING", shot=shot, attempt=attempt, artifact=artifact,
            )
        ]
        # 配音失败**单独上报**，不能只写服务端日志。
        #
        # 这里的教训很具体：用户配好了 TTS、成片却是哑的，而失败只躺在 AI 服务的
        # 日志里 —— 界面上没有任何提示，于是排查方向完全跑偏（怀疑音色、怀疑播放器、
        # 怀疑音量），而真正的原因是服务端一句"并发配额不足"或"鉴权失败"。
        # 降级本身是对的（画面才是主体），但**降级必须可见**。
        if tts_error:
            events.append(
                make_event(
                    state, node=NODE_RENDER,
                    message=(
                        f"镜头 #{shot.index} 配音未生成，该镜头将没有旁白：{tts_error}"
                    ),
                    status="CRITIQUING", shot=shot, attempt=attempt,
                    payload_json=json.dumps({"tts_failed": True, "tts_error": tts_error},
                                            ensure_ascii=False),
                )
            )

        # 节奏检查的结论也上报：它是**算出来的**，而审查的判断是观感。
        # 两者不一致时（审查通过但系统算出有静止时段），事件流里要能看出这件事。
        motion_reports = dict(state.get("motion_reports") or {})
        if motion_report.ok:
            motion_reports.pop(shot.shot_id, None)
        else:
            motion_reports[shot.shot_id] = _motion_feedback(motion_report, shot)
            events.append(
                make_event(
                    state, node=NODE_RENDER,
                    message=(
                        f"节奏检查：镜头 #{shot.index} 有 {len(motion_report.static_spans)} "
                        f"段时间画面没有变化（最早一段 "
                        f"{motion_report.static_spans[0][0]:.1f}s~"
                        f"{motion_report.static_spans[0][1]:.1f}s）"
                    ),
                    status="CRITIQUING", shot=shot, attempt=attempt,
                    payload_json=json.dumps(
                        {
                            "pacing_static_spans": [
                                {"start_sec": round(a, 2), "end_sec": round(b, 2),
                                 "change_ratio": round(c, 5)}
                                for a, b, c in motion_report.static_spans
                            ],
                            "pacing_ratios": [round(r, 5) for r in motion_report.ratios],
                        },
                        ensure_ascii=False,
                    ),
                )
            )

        return {
            "artifacts": artifacts,
            "frame_signatures": {**(state.get("frame_signatures") or {}), shot.shot_id: signatures},
            "review_samples": {**(state.get("review_samples") or {}), shot.shot_id:
                               [{"path": s.path, "ts": s.ts} for s in samples]},
            "repair_prechecks": {**(state.get("repair_prechecks") or {}), shot.shot_id: prechecks},
            "motion_reports": motion_reports,
            "render_error": "",
            "route_hint": HINT_CRITIQUE,
            "events": events,
        }

    # ==================================================================
    # critique：VLM 审查
    # ==================================================================


    def _synthesize_narration(self, shot: "ShotSpec", out_dir: Path) -> tuple[str, str]:
        """为该镜头合成配音。

        返回 `(音频路径, 失败原因)`：成功时原因为空串。

        失败**只降级不抛出**：画面才是主体，没有旁白的镜头仍是可用产物，
        而「因为 TTS 抖动就丢掉整个镜头」是把外部依赖的问题升级成内容事故。
        但**失败原因必须回传给调用方**，由它写进事件流 —— 只记服务端日志
        是不够的：用户配好了 TTS、成片却是哑的，界面上却毫无提示，
        排查方向会完全跑偏（怀疑音色、播放器、音量），而真正的原因
        往往只是服务端一句「并发配额不足」。
        """
        provider = self.deps.tts
        if provider is None:
            # 没配 TTS 是**刻意的配置选择**（不是失败）：成片本来就不该有旁白，
            # 因此不报错也不上报 —— 否则每一帧都刷一条"失败"会把真正的问题淹掉。
            return "", ""

        narration = (shot.narration or "").strip()
        if not narration:
            return "", ""

        # resolve：这个路径要跨进程交给 Go worker，两边的 CWD 不同，
        # 相对路径在生产端看着没问题、到消费端就是「文件不存在」。
        out_path = out_dir.resolve() / "narration.mp3"
        try:
            # 带重试：TTS 是网络调用，实测会遇到「连接被 reset」这类瞬时失败。
            # 只试一次会让大部分镜头悄悄失去配音（成片莫名没声音）。
            result = synthesize_with_retry(
                provider,
                narration,
                out_path=out_path,
                attempts=getattr(self.deps.settings, "tts_max_attempts", 3),
                backoff_sec=getattr(self.deps.settings, "tts_retry_backoff_sec", 1.0),
            )
        except Exception as exc:  # noqa: BLE001 - TTSError 或适配器未预期的异常
            reason = str(exc)[:300] or type(exc).__name__
            logger.warning(
                "配音合成失败，该镜头将没有配音",
                extra={
                    "shot_id": shot.shot_id,
                    "provider": getattr(provider, "name", "?"),
                    "retryable": getattr(exc, "retryable", None),
                    "error": reason,
                },
            )
            return "", reason

        # 时间戳 sidecar：Go 侧据此把字幕对到真实句子起止。
        # 不给时间戳的服务商也写一份（marks 为空），这样 Go 能区分
        # 「这家本来就不给时间戳」与「文件丢了」—— 两者的排查方向完全不同。
        try:
            write_marks_sidecar(out_path, result)
        except OSError as exc:
            logger.warning(
                "时间戳 sidecar 写入失败，字幕将回退到按镜头时长对齐",
                extra={"shot_id": shot.shot_id, "error": str(exc)[:200]},
            )

        logger.info(
            "配音已合成",
            extra={
                "shot_id": shot.shot_id,
                "provider": result.provider,
                "duration_sec": round(result.duration_sec, 2),
                "marks": len(result.marks),
            },
        )
        return str(result.audio_path), ""

    @traced_node("critique")
    def critique(self, state: PipelineState) -> dict[str, Any]:
        """用 VLM 审查抽帧，决定通过、重做还是转人工。"""
        shot = current_shot(state)
        if shot is None:
            return {"route_hint": HINT_DONE, "finished": True}

        artifact = (state.get("artifacts") or {}).get(shot.shot_id)
        if artifact is None:
            # 正常情况下不会发生（render 成功才会到 critique）。
            # 真发生说明状态被外部篡改或续跑时 checkpoint 不完整 —— 转人工最安全。
            logger.error("critique 缺少渲染产物，转人工", extra={"shot_id": shot.shot_id})
            return {
                "route_hint": HINT_HUMAN,
                "events": [
                    make_event(
                        state, node=NODE_CRITIQUE, shot=shot,
                        message="缺少渲染产物，无法审查，已转人工",
                        status="AWAITING_HUMAN", error="missing artifact",
                    )
                ],
            }

        style = state.get("style_guide") or StyleGuide()
        attempt = shot_attempt(state, shot.shot_id)
        started = time.monotonic()

        outcome = self.deps.critic.review(
            shot=shot,
            artifact=artifact,
            style_guide=style,
            attempt=attempt,
            previous_feedback=_collect_feedback(state, shot.shot_id),
            previous_review=(state.get("feedback") or {}).get(shot.shot_id),
            repair_prechecks=(state.get("repair_prechecks") or {}).get(shot.shot_id, []),
        )

        feedback_map = dict(state.get("feedback") or {})
        previous_feedback = feedback_map.get(shot.shot_id)
        if outcome.degraded:
            outcome.feedback = merge_repairs(previous_feedback, outcome.feedback, [], set())
        old_ids = {t.task_id for t in previous_feedback.repair_tasks} if previous_feedback else set()
        samples = (state.get("review_samples") or {}).get(shot.shot_id, [])
        for task in outcome.feedback.repair_tasks:
            if task.task_id not in old_ids and task.frame_indices and task.end_sec == 0:
                times = [samples[i - 1]["ts"] for i in task.frame_indices if i <= len(samples)]
                if times:
                    task.start_sec, task.end_sec = min(times), max(times)
        feedback_map[shot.shot_id] = outcome.feedback
        stagnation = dict(state.get("revision_stagnation") or {})
        stagnated = _same_unresolved_feedback(previous_feedback, outcome.feedback)
        if stagnated:
            stagnation[shot.shot_id] = (
                "连续两轮审查提出相同问题且得分没有明显提升。"
                "下一版必须重构相关画面元素或阶段，让指定问题在抽帧中可见地消失；"
                "不要重复微调字号、时长或颜色等未解决问题的参数。"
            )
        else:
            stagnation.pop(shot.shot_id, None)

        # 记录本镜头的历次得分，供 revise 判断"重做到底有没有让画面变好"。
        # 只留最后一次的 feedback 不足以判断趋势 —— 而趋势正是"该不该继续重试"
        # 唯一有信息量的依据。
        history_map = {k: list(v) for k, v in (state.get("score_history") or {}).items()}
        history_map.setdefault(shot.shot_id, []).append(float(outcome.feedback.score))

        if outcome.degraded:
            hint, status = HINT_HUMAN, "AWAITING_HUMAN"
            message = f"无法自动审查，已转人工：{outcome.degradation_reason[:120]}"
        elif outcome.passed:
            hint, status = HINT_OK, "APPROVED"
            message = f"审查通过（{outcome.feedback.score:.2f}）"
        else:
            hint, status = HINT_RETRY, "REJECTED"
            first = outcome.feedback.suggestions[0] if outcome.feedback.suggestions else ""
            message = f"审查未通过（{outcome.feedback.score:.2f}）：{first[:120]}"
            if stagnated:
                message += "；连续两轮问题相同且无明显提升，下一轮须重构相关画面"

        logger.info(
            "critique 完成",
            extra={
                "shot_id": shot.shot_id, "attempt": attempt, "passed": outcome.passed,
                "degraded": outcome.degraded, "score": round(outcome.feedback.score, 3),
                "elapsed_sec": round(time.monotonic() - started, 2),
            },
        )
        return {
            "feedback": feedback_map,
            "revision_stagnation": stagnation,
            "score_history": history_map,
            "route_hint": hint,
            "events": [
                make_event(
                    state, node=NODE_CRITIQUE, message=message, status=status,
                    shot=shot, attempt=attempt, feedback=outcome.feedback,
                    artifact=artifact,
                    payload_json=_repair_metrics(state, shot.shot_id, outcome.feedback, previous_feedback),
                )
            ],
            "total_tokens": self.deps.llm.usage.total_tokens,
        }

    # ==================================================================
    # revise：熔断判断（重做的唯一入口）
    # ==================================================================

    @traced_node("revise")
    def revise(self, state: PipelineState) -> dict[str, Any]:
        """决定"还能不能再试一次"。

        这是**成本控制的核心闸门**：没有它，一个永远渲染不好的镜头会无限烧钱。
        熔断后不是让整个任务失败，而是把这个镜头标记为转人工并**继续下一个** ——
        其余镜头可能是好的，整片仍有交付价值。
        """
        shot = current_shot(state)
        if shot is None:
            return {"route_hint": HINT_DONE, "finished": True}

        attempt = shot_attempt(state, shot.shot_id)
        max_attempts = int(state.get("max_attempts_per_shot", 3) or 3)

        # 「评审判负」与「渲染报错」必须分开对待：
        #   * 渲染报错通常是环境/瞬时问题，重试确实有机会好；
        #   * 评审判负说明画面本身不合意，只有**换一段代码**才可能改变画面。
        # 而程序化引擎（stock：ffmpeg 渐变 + 一行标题）压根没有代码，
        # 它的输出由固定参数决定，重跑必然得到逐像素相同的画面。
        # 对这种情况走重试是纯浪费：白烧 (max_attempts - 1) 轮渲染 + VLM 调用，
        # 最后仍然落到人工。所以直接转人工，并把"重试无用"的原因写进事件里。
        feedback = (state.get("feedback") or {}).get(shot.shot_id)
        engine = shot.engine.value if shot.engine else ""
        if (
            feedback is not None
            and not feedback.passed
            and engine
            and engine not in LLM_ENGINES
        ):
            issues = "；".join(feedback.issues[:3]) or "未给出具体原因"
            logger.warning(
                "程序化引擎的画面不会被重试改变，跳过重试直接转人工",
                extra={
                    "shot_id": shot.shot_id,
                    "engine": engine,
                    "attempt": attempt,
                    "score": round(feedback.score, 3),
                },
            )
            return {
                "route_hint": HINT_HUMAN,
                "events": [
                    make_event(
                        state, node=NODE_REVISE, shot=shot, attempt=attempt,
                        message=(
                            f"{engine} 引擎按固定参数程序化出图、没有可修改的代码，"
                            f"重试不会产生不同画面，直接转人工（审查得分 {feedback.score:.2f}）"
                        ),
                        status="AWAITING_HUMAN",
                        error=issues[:500],
                    )
                ],
            }

        # 「重做没有让画面变好」也要熔断。
        #
        # 实测依据（一条 8 镜头的真实任务）：两个镜头各重试到上限，三次拿到的
        # 审查意见**一字不差**，分数还一路走低（0.64→0.45→0.45、0.32→0.30→0.36），
        # 最后都靠人工放行。那两次重做是纯粹的浪费：一次渲染 + 一次编码调用
        # + 一次 VLM 调用，而且因为流水线是串行的，它还把后面的镜头一起拖住。
        #
        # 判据刻意**保守**，两条同时满足才提前熔断：
        #   * 最新一次得分没有超过此前的最高分（完全没有进步）；
        #   * 最新得分仍明显低于及格线（`_HOPELESS_SCORE`）—— 离及格很近的
        #     镜头（例如 0.6 上下波动）仍有靠下一轮翻盘的可能，不该被掐掉。
        # 只满足"没进步"就熔断会把"0.55→0.58→0.95"这类慢热镜头误杀。
        history = list((state.get("score_history") or {}).get(shot.shot_id) or [])
        if active_repairs(feedback):
            progress, escalations, exhausted = update_repair_progress(
                feedback, (state.get("repair_progress") or {}).get(shot.shot_id, {}), attempt, max_attempts)
            progress_map = {**(state.get("repair_progress") or {}), shot.shot_id: progress}
            if exhausted or attempt >= max_attempts:
                reason = "策略升级后问题仍未解决" if exhausted else f"已尝试 {attempt} 次仍未通过"
                return {
                    "repair_progress": progress_map, "route_hint": HINT_HUMAN,
                    "events": [make_event(state, node=NODE_REVISE, shot=shot, attempt=attempt,
                        message=reason + "，转人工处理；未解决：" + "、".join(t.task_id for t in active_repairs(feedback)),
                        status="AWAITING_HUMAN", feedback=feedback,
                        artifact=(state.get("artifacts") or {}).get(shot.shot_id),
                        payload_json=_repair_metrics(state, shot.shot_id, feedback, progress=progress,
                                                     handoff=True))],
                }
            stagnation = dict(state.get("revision_stagnation") or {})
            if escalations:
                strategies = "\n".join(f"- [{t.task_id}] {REPAIR_STRATEGIES[t.category]}；验收：{t.acceptance}"
                                       for t in escalations)
                stagnation[shot.shot_id] = render_prompt("repair_escalation", strategies=strategies)
            return {
                "repair_progress": progress_map, "revision_stagnation": stagnation,
                "route_hint": HINT_RETRY,
                "events": [make_event(state, node=NODE_REVISE, shot=shot, attempt=attempt,
                    message=("连续无改善，升级修复策略：" + "、".join(t.task_id for t in escalations)
                             if escalations else f"按问题验收结果重做（第 {attempt + 1}/{max_attempts} 次）"),
                    status="RETRYING", payload_json=_repair_metrics(state, shot.shot_id, feedback,
                                                                    progress=progress))],
            }
        if len(history) >= 2:
            latest = history[-1]
            best_before = max(history[:-1])
            if latest <= best_before and latest < _HOPELESS_SCORE:
                logger.warning(
                    "重做没有让画面变好，提前转人工（省下一轮渲染与两次模型调用）",
                    extra={
                        "shot_id": shot.shot_id,
                        "attempt": attempt,
                        "history": [round(s, 3) for s in history],
                    },
                )
                return {
                    "route_hint": HINT_HUMAN,
                    "events": [
                        make_event(
                            state, node=NODE_REVISE, shot=shot, attempt=attempt,
                            message=(
                                f"连续 {len(history)} 次审查得分没有进步"
                                f"（{'→'.join(f'{s:.2f}' for s in history)}），"
                                f"重做下去大概率还是同一结果，提前转人工"
                            ),
                            status="AWAITING_HUMAN",
                            error=state.get("render_error", "")[:500],
                        )
                    ],
                }

        if attempt >= max_attempts:
            logger.warning(
                "镜头重试已达上限，转人工",
                extra={"shot_id": shot.shot_id, "attempt": attempt, "max": max_attempts},
            )
            return {
                "route_hint": HINT_HUMAN,
                "events": [
                    make_event(
                        state, node=NODE_REVISE, shot=shot, attempt=attempt,
                        message=f"已尝试 {attempt} 次仍未通过，转人工处理（不再自动重试）",
                        status="AWAITING_HUMAN",
                        error=state.get("render_error", "")[:500],
                    )
                ],
            }

        logger.info(
            "准备重做该镜头",
            extra={"shot_id": shot.shot_id, "attempt": attempt, "max": max_attempts},
        )
        return {
            "route_hint": HINT_RETRY,
            "events": [
                make_event(
                    state, node=NODE_REVISE, shot=shot, attempt=attempt,
                    message=f"带着反馈重做（第 {attempt + 1}/{max_attempts} 次尝试）",
                    status="RETRYING",
                )
            ],
        }

    # ==================================================================
    # advance：推进到下一镜头
    # ==================================================================

    @traced_node("advance")
    def advance(self, state: PipelineState) -> dict[str, Any]:
        """游标前移；全部镜头处理完毕后收尾。"""
        shots = state.get("shots") or []
        cursor = int(state.get("cursor", 0)) + 1

        if cursor >= len(shots):
            ratio = progress_ratio({**state, "cursor": cursor})  # type: ignore[arg-type]
            logger.info(
                "流水线全部镜头处理完毕",
                extra={
                    "job_id": state.get("job_id", ""),
                    "shots": len(shots),
                    "progress": ratio,
                    "total_tokens": self.deps.llm.usage.total_tokens,
                },
            )
            return {
                "cursor": cursor,
                "finished": True,
                "route_hint": HINT_DONE,
                "events": [
                    make_event(
                        state, node=NODE_ADVANCE,
                        message=f"全部 {len(shots)} 个分镜处理完成，通过率 {ratio:.0%}",
                    )
                ],
                "total_tokens": self.deps.llm.usage.total_tokens,
            }

        next_shot = shots[cursor]
        return {
            "cursor": cursor,
            "finished": False,
            "route_hint": HINT_NEXT,
            "events": [
                make_event(
                    state, node=NODE_ADVANCE, shot=next_shot,
                    message=f"进入第 {cursor + 1}/{len(shots)} 个分镜（{next_shot.tag.value}）",
                    status="PENDING",
                )
            ],
        }


# ===========================================================================
# 条件边
# ===========================================================================
#
# 条件边只做一件事：读取节点声明的 route_hint，校验它是否在允许集合内，
# 然后返回目标节点名。所有"判断逻辑"都在节点里，边只负责**路由**。
# 这样控制流是显式且可测的（见 tests/test_graph_routing.py）。


def _route(state: PipelineState, allowed: dict[str, str], default: str) -> str:
    hint = state.get("route_hint", "")
    target = allowed.get(hint)
    if target is None:
        # 未知 hint 说明节点实现与边配置不一致 —— 这是**编程错误**，
        # 必须吵闹地失败，而不是悄悄走默认分支（那会表现为"流水线莫名结束"）。
        if hint:
            raise PipelineError(f"未登记的 route_hint={hint!r}，允许值：{sorted(allowed)}")
        return default
    return target


def route_after_code(state: PipelineState) -> str:
    """code -> render（成功）或 revise（静态检查失败 / 模型调用失败）。"""
    return _route(state, {HINT_RENDER: NODE_RENDER, HINT_RETRY: NODE_REVISE}, NODE_RENDER)


def route_after_render(state: PipelineState) -> str:
    """render -> critique（成功）/ revise（可重试失败）/ advance（不可重试 -> 转人工）。"""
    return _route(
        state,
        {HINT_CRITIQUE: NODE_CRITIQUE, HINT_RETRY: NODE_REVISE, HINT_HUMAN: NODE_ADVANCE},
        NODE_CRITIQUE,
    )


def route_after_critique(state: PipelineState) -> str:
    """critique -> advance（通过 / 转人工）或 revise（不通过，继续重做）。"""
    return _route(
        state,
        {HINT_OK: NODE_ADVANCE, HINT_RETRY: NODE_REVISE, HINT_HUMAN: NODE_ADVANCE},
        NODE_ADVANCE,
    )


def route_after_revise(state: PipelineState) -> str:
    """revise -> code（额度未尽）或 advance（已熔断）。"""
    return _route(state, {HINT_RETRY: NODE_CODE, HINT_HUMAN: NODE_ADVANCE}, NODE_ADVANCE)


def route_after_advance(state: PipelineState) -> str:
    """advance -> code（还有镜头）或 END（完成）。"""
    return _route(state, {HINT_NEXT: NODE_CODE, HINT_DONE: "__end__"}, "__end__")


# ===========================================================================
# 工具
# ===========================================================================


def _collect_feedback(state: PipelineState, shot_id: str) -> str:
    """汇总该镜头当前可用的全部反馈。

    三类来源合并成一段文本回灌给编码智能体：
      * VLM 审查意见（issues + suggestions）
      * 人类打回意见（HITL）
      * 渲染器的技术错误（编译失败、超时、内存超限）

    编码侧无需区分来源 —— 它只需要知道"上一版哪里不行"。
    这也是为什么 :class:`CriticFeedback` 让 VLM 与人类共用同一结构。
    """
    parts: list[str] = []

    feedback = (state.get("feedback") or {}).get(shot_id)
    if isinstance(feedback, CriticFeedback) and not feedback.passed:
        repairs = format_repairs(feedback)
        if repairs:
            parts.append(repairs)
        elif feedback.issues:
            parts.append("【画面问题】\n" + "\n".join(f"- {i}" for i in feedback.issues[:6]))
        if not repairs and feedback.suggestions:
            parts.append(
                "【必须落实的修改】\n" + "\n".join(f"- {s}" for s in feedback.suggestions[:6])
            )

    human = (state.get("human_feedback") or {}).get(shot_id)
    if human:
        # 人工意见优先级最高：它是人看过成片后给出的判断。
        parts.append(f"【人工审核意见（优先级最高）】\n{human}")

    # 节奏检查的结论：**具体的时段**，由系统算出而不是观感。
    #
    # 位置放在 VLM 意见**之后**、技术错误之前：它是对"节奏"这一项的具体化，
    # 应当与审查意见相邻，读的人能看出"审查说节奏不好"到底指哪几秒。
    motion = (state.get("motion_reports") or {}).get(shot_id)
    if motion:
        parts.append(motion)

    stagnation = (state.get("revision_stagnation") or {}).get(shot_id)
    if stagnation:
        parts.append(f"【重复审查无进展】\n{stagnation}")

    render_error = (state.get("render_error") or "").strip()
    if render_error:
        heading = (
            "【无效重做反馈】"
            if render_error.startswith(("新源码与上一轮", "新旧审查抽帧", "本轮修复任务"))
            else "【上一次渲染的技术错误】"
        )
        parts.append(f"{heading}\n{render_error[:1200]}")

    return "\n\n".join(parts)


def _repair_metrics(
    state: PipelineState, shot_id: str, feedback: CriticFeedback,
    previous: CriticFeedback | None = None, *, progress: dict[str, Any] | None = None,
    handoff: bool = False,
) -> str:
    old_resolved = {t.task_id for t in previous.repair_tasks if t.status == "resolved"} if previous else set()
    resolved = [t.task_id for t in feedback.repair_tasks if t.status == "resolved"]
    unresolved = [t for t in feedback.repair_tasks if t.status != "resolved" and t.severity != "advisory"]
    payload = {"repair_metrics": {
        "attempt": shot_attempt(state, shot_id), "resolved_ids": resolved,
        "newly_resolved_ids": [i for i in resolved if i not in old_resolved],
        "unresolved_ids": [t.task_id for t in unresolved],
        "progress": progress or (state.get("repair_progress") or {}).get(shot_id, {}),
    }}
    if handoff:
        payload["repair_handoff"] = {
            "unresolved_tasks": [t.model_dump() for t in unresolved],
            "evidence": (state.get("repair_prechecks") or {}).get(shot_id, []),
            "suggested_strategies": {t.task_id: REPAIR_STRATEGIES[t.category] for t in unresolved},
        }
    return json.dumps(payload, ensure_ascii=False)


def _code_patch(shot_id: str, code: str, language: str, *, generation_quality: dict[str, Any] | None = None) -> str:
    """生成跨语言状态补丁（Python -> Go）。

    约定见 docs/API.md：``payload_json`` 里的 ``patch`` 会被 Go 侧应用到对应镜头。
    这样 HITL 重做时，Go 手里的 shot 已经带着最新代码，
    ``ReviseShot`` 就能做"基于上一版修改"而不是"从零重写"。
    """
    payload: dict[str, Any] = {"patch": {"shot_id": shot_id, "code": code, "language": language}}
    if generation_quality is not None:
        payload["generation_quality"] = generation_quality
    return json.dumps(
        payload,
        ensure_ascii=False,
    )


def _tag_distribution(shots: list[ShotSpec]) -> dict[str, int]:
    """标签分布。用于观测"模型是不是把所有镜头都打成了 AMBIENCE"。"""
    dist: dict[str, int] = {}
    for shot in shots:
        key = shot.tag.value
        dist[key] = dist.get(key, 0) + 1
    return dist
