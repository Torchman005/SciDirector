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
   而 ``artifacts`` / ``feedback`` / ``attempts`` 是**普通字典**，返回时**整体替换**，
   因此每次都必须返回完整副本 —— 少写一个键就会静默丢失已有产物。
2. ``route_hint`` 是控制流的唯一依据（见 state.py 的说明）。
"""

from __future__ import annotations

import functools

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agents.coder import CoderAgent
from ..agents.critic import CriticAgent
from ..agents.director import DirectorAgent
from ..config import Settings
from ..llm import LLMClient, LLMError
from ..logging import get_logger
from ..media import MediaToolError, extract_frames
from ..pbconv import shots_payload_json
from ..renderer import Renderer, RendererError, RenderRequest, build_renderer
from ..sandbox.runner import SandboxRunner
from ..tts.base import synthesize_with_retry, write_marks_sidecar
from ..schemas import CriticFeedback, RenderArtifact, ShotSpec, StyleGuide
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

        ok = result.policy_ok
        engine_name = shot.engine.value if shot.engine else "?"
        message = (
            f"已生成 {engine_name} 代码（{len(result.code)} 字符，第 {attempt} 次尝试）"
            if ok
            else f"生成的代码未通过静态检查：{result.policy_summary[:200]}"
        )

        # 氛围镜头的标题要落到 shot.meta，渲染节点才会用它绘图。
        shots = list(state.get("shots") or [])
        if result.overlay_text and 0 <= shot.index < len(shots):
            updated = shot.model_copy(update={
                "meta": {**shot.meta, "overlay_text": result.overlay_text}
            })
            shots[shot.index] = updated

        events = [
            make_event(
                state, node=NODE_CODE, message=message, status="GENERATING",
                shot=shot, attempt=attempt, error="" if ok else result.policy_summary[:500],
                # 跨语言补丁：把生成的代码同步给 Go，
                # 使 HITL 重做时能带着上一版代码重写。
                payload_json=_code_patch(shot.shot_id, result.code, result.artifact.language),
            )
        ]

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
        out_dir = Path(self.deps.settings.sandbox_work_dir) / job_id / f"shot_{shot.index:03d}"

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
        frames: list[str] = []
        try:
            frames = extract_frames(
                result.video_path,
                out_dir / "frames",
                self.deps.runner,
                count=self.deps.settings.critic_frame_samples,
                duration_sec=result.duration_sec,
            )
        except MediaToolError as exc:
            logger.warning(
                "抽帧失败，审查将降级",
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
        artifact.audio_path = self._synthesize_narration(shot, out_dir)

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
        return {
            "artifacts": artifacts,
            "render_error": "",
            "route_hint": HINT_CRITIQUE,
            "events": [
                make_event(
                    state, node=NODE_RENDER,
                    message=(
                        f"渲染完成：{result.duration_sec:.1f}s / "
                        f"{result.width}x{result.height} / {len(frames)} 帧抽帧"
                    ),
                    status="CRITIQUING", shot=shot, attempt=attempt, artifact=artifact,
                )
            ],
        }

    # ==================================================================
    # critique：VLM 审查
    # ==================================================================


    def _synthesize_narration(self, shot: "ShotSpec", out_dir: Path) -> str:
        """为该镜头合成配音，返回音频路径；未启用或失败时返回空串。

        失败**只降级不抛出**：画面才是主体，没有旁白的镜头仍是可用产物，
        而「因为 TTS 抖动就丢掉整个镜头」是把外部依赖的问题升级成内容事故。
        但一定留痕 —— 成片没声音是可见的质量差异，静默降级会让人误以为
        「TTS 接好了却没生效」，排查方向会完全跑偏。
        """
        provider = self.deps.tts
        if provider is None:
            return ""

        narration = (shot.narration or "").strip()
        if not narration:
            return ""

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
            logger.warning(
                "配音合成失败，该镜头将没有配音",
                extra={
                    "shot_id": shot.shot_id,
                    "provider": getattr(provider, "name", "?"),
                    "retryable": getattr(exc, "retryable", None),
                    "error": str(exc)[:300],
                },
            )
            return ""

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
        return str(result.audio_path)

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
        )

        feedback_map = dict(state.get("feedback") or {})
        feedback_map[shot.shot_id] = outcome.feedback

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
            "route_hint": hint,
            "events": [
                make_event(
                    state, node=NODE_CRITIQUE, message=message, status=status,
                    shot=shot, attempt=attempt, feedback=outcome.feedback,
                    artifact=artifact,
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
        if feedback.issues:
            parts.append("【画面问题】\n" + "\n".join(f"- {i}" for i in feedback.issues[:6]))
        if feedback.suggestions:
            parts.append(
                "【必须落实的修改】\n" + "\n".join(f"- {s}" for s in feedback.suggestions[:6])
            )

    human = (state.get("human_feedback") or {}).get(shot_id)
    if human:
        # 人工意见优先级最高：它是人看过成片后给出的判断。
        parts.append(f"【人工审核意见（优先级最高）】\n{human}")

    render_error = (state.get("render_error") or "").strip()
    if render_error:
        parts.append(f"【上一次渲染的技术错误】\n{render_error[:1200]}")

    return "\n\n".join(parts)


def _code_patch(shot_id: str, code: str, language: str) -> str:
    """生成跨语言状态补丁（Python -> Go）。

    约定见 docs/API.md：``payload_json`` 里的 ``patch`` 会被 Go 侧应用到对应镜头。
    这样 HITL 重做时，Go 手里的 shot 已经带着最新代码，
    ``ReviseShot`` 就能做"基于上一版修改"而不是"从零重写"。
    """
    return json.dumps(
        {"patch": {"shot_id": shot_id, "code": code, "language": language}},
        ensure_ascii=False,
    )


def _tag_distribution(shots: list[ShotSpec]) -> dict[str, int]:
    """标签分布。用于观测"模型是不是把所有镜头都打成了 AMBIENCE"。"""
    dist: dict[str, int] = {}
    for shot in shots:
        key = shot.tag.value
        dist[key] = dist.get(key, 0) + 1
    return dist
