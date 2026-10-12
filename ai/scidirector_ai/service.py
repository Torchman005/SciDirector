"""业务门面：HTTP 与 gRPC 两条入口共用同一套逻辑。

为什么要有这一层？
* 避免 HTTP handler 与 gRPC servicer 各写一遍业务逻辑（那必然导致行为漂移）；
* 让业务逻辑可以在没有网络的情况下被单测直接调用；
* 传输层的关注点（proto 转换、状态码、tracing）被隔离在各自的适配器里。

阶段二后全部能力均已实现：
    ``run_pipeline``   完整 LangGraph 流水线（服务端流式）
    ``plan_script``    只跑导演智能体
    ``generate_shot``  编码 + 沙盒渲染单个镜头
    ``critique_shot``  VLM 审查单次产物
    ``revise_shot``    人工意见回灌 -> 重写 -> 重渲 -> 自动复审
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import __version__
from .agents.coder import CoderAgent
from .agents.critic import CriticAgent
from .agents.director import DirectorAgent
from .config import Settings, engine_availability, get_settings
from .diagnostics import error_evidence
from .recovery import recovery_instruction, preserved_science
from .graph.builder import PipelineRunner
from .llm import LLMClient, LLMError
from .logging import get_logger
from .media import MediaToolError, FrameSample, extract_frames, frame_sample_times
from .repair_evidence import precheck_repairs, skip_full_review
from .review_tasks import active_repairs
from .scene_revision import parse_exact_revision
from .renderer import RendererError, RenderRequest, build_renderer, renderer_availability
from .sandbox.runner import SandboxRunner
from .tts.factory import build_tts_provider
from .tts.base import synthesize_with_retry, write_marks_sidecar
from .schemas import (
    CriticFeedback,
    FeedbackSource,
    JobRequest,
    RenderArtifact,
    ScriptPlan,
    ShotSpec,
    StyleGuide,
)

logger = get_logger(__name__)

# 进程启动时间，用于 /healthz 报告 uptime。
_STARTED_AT = time.time()


class ServiceUnavailable(RuntimeError):
    """依赖不可用（模型、沙盒、存储等），重试可能成功。"""


class RenderFailed(RuntimeError):
    """渲染失败。

    单独成类是为了让 gRPC 适配层把它映射成 ``FAILED_PRECONDITION``
    而不是 ``UNAVAILABLE``：前者**不会**触发 Go 侧 Asynq 的重试，
    而"缺 Manim/字体/浏览器"这类问题重试多少次都一样，
    盲目重试只会浪费资源并污染告警。
    """


    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class HealthStatus:
    """健康与能力报告。"""

    healthy: bool = True
    version: str = __version__
    llm_provider: str = ""
    vlm_model: str = ""
    sandbox_ready: bool = False
    capabilities: list[str] = field(default_factory=list)
    uptime_sec: int = 0
    toolchain: dict[str, bool] = field(default_factory=dict)
    #: 每个渲染引擎的工具链是否就绪（键为引擎名）。
    engines: dict[str, bool] = field(default_factory=dict)
    #: checkpoint 后端（memory / postgres），决定进程重启后能否续跑。
    checkpoint_backend: str = "unknown"


class PipelineService:
    """SciDirector AI 大脑的业务门面。

    线程安全性：实例本身基本无状态；``LLMClient`` 的用量计数是唯一的共享
    可变状态，其轻微误差不影响正确性。gRPC 线程池会并发调用本类的方法。
    """

    def __init__(self, settings: Settings | None = None, llm: LLMClient | None = None) -> None:
        self.settings = settings or get_settings()
        self.llm = llm or LLMClient(self.settings)
        self._runner: PipelineRunner | None = None
        self._director: DirectorAgent | None = None
        self._coder: CoderAgent | None = None
        self._critic: CriticAgent | None = None
        self._sandbox: SandboxRunner | None = None

    # ------------------------------------------------------------------
    # 惰性构造的重型组件
    # ------------------------------------------------------------------
    #
    # 这些组件都持有资源（Postgres 连接、并发闸门、渲染器缓存），
    # 因此进程内只应有一份。用 property 惰性构造，让"仅调用 health"的场景
    # （例如 k8s 探针）不必付出初始化代价。

    @property
    def director(self) -> DirectorAgent:
        if self._director is None:
            self._director = DirectorAgent(self.llm)
        return self._director

    @property
    def coder(self) -> CoderAgent:
        if self._coder is None:
            self._coder = CoderAgent(self.llm, self.settings)
        return self._coder

    @property
    def critic(self) -> CriticAgent:
        if self._critic is None:
            self._critic = CriticAgent(self.llm, self.settings)
        return self._critic

    @property
    def runner(self) -> SandboxRunner:
        if self._sandbox is None:
            self._sandbox = SandboxRunner(
                self.settings.sandbox_network_isolation,
                self.settings.sandbox_read_only,
                self.settings.sandbox_seccomp,
            )
        return self._sandbox

    @property
    def pipeline(self) -> PipelineRunner:
        if self._runner is None:
            self._runner = PipelineRunner(self.settings, llm=self.llm)
        return self._runner

    def render_presenter(self, **kwargs: Any) -> dict[str, Any]:
        from .presenter import render_presenter
        return render_presenter(self.settings, self.runner, **kwargs)

    def close(self) -> None:
        """释放持有外部资源的组件。进程退出时调用。"""
        if self._runner is not None:
            self._runner.close()
            self._runner = None

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------

    def health(self) -> HealthStatus:
        """返回服务与沙盒的就绪状态。

        ``sandbox_ready`` 的判定：**至少有一类渲染引擎的工具链可用**
        （``any(engines.values())``），而不是「所有引擎都可用」。

        为什么是「至少一个」：只有 ffmpeg 的环境依然能产出氛围镜头，
        把这种情况判成 not-ready 会让编排层摘掉一个**确实能干活**的实例。
        缺失的引擎通过 ``capabilities`` 里的 ``engine:<名字>=missing`` 单独暴露。

        需要明确的边界：本函数回答的是**工具链就绪度**，
        **不是**「渲染器是否已实现」。
        """
        toolchain = self.settings.toolchain_report()
        engines = engine_availability(toolchain)

        capabilities = ["plan", "code", "critique"]
        for role, reason in self.llm.model_faults().items():
            capabilities.append(f"model:{role}=unavailable:{reason}")
        from .presenter import runtime_paths
        if self.settings.tts_provider not in {"", "none"} and toolchain.get("ffmpeg") and any(engines.get(e) for e in ("d3", "motion", "code_anim")):
            capabilities.append("presenter:portrait=ok")
        else:
            capabilities.append("presenter:portrait=missing")
        try:
            runtime_paths(self.settings)
            if self.settings.tts_provider in {"", "none"}:
                raise ValueError("TTS 未启用")
            capabilities.append("presenter:live2d=ok")
        except ValueError:
            capabilities.append("presenter:live2d=missing")
        if any(engines.values()):
            capabilities.append("render")
        if toolchain.get("ffmpeg"):
            capabilities.append("compose")
        for name, ok in sorted(engines.items()):
            capabilities.append(f"engine:{name}={'ok' if ok else 'missing'}")
        # "pipeline" 表示 run_pipeline 已实现 —— 冒烟脚本据此自动切换
        # 「阶段一预期 UNIMPLEMENTED」与「阶段二预期真实结果」。
        capabilities.append("pipeline")
        # 文本与视觉**分开报**：它们可以是不同的服务商，而且"文本是某家、
        # 视觉没配"会产生完全不同的后果（每个镜头都转人工）。合成一个字段
        # 就看不出来了。
        text_t, vision_t = self.llm.text, self.llm.vision
        if self.llm.is_mock:
            # mock 时**不能**报成真实服务商：那会让人以为"已经接上 OpenAI 了"，
            # 而实际上一次真实调用都没有。括号里保留"配置成了哪家"，
            # 便于判断是"故意 mock"还是"忘了配密钥"。
            capabilities.append(f"llm:text=mock(配置为 {text_t.provider})")
            capabilities.append("mock-llm")
        else:
            capabilities.append(f"llm:text={text_t.provider}/{text_t.model}")
        if vision_t.supports_vision and vision_t.usable:
            capabilities.append(f"llm:vision={vision_t.provider}/{vision_t.model}")
        else:
            # 明确报"没有视觉"，而不是省掉这一项 —— 省掉的话，
            # 运维只能从"每个镜头都转人工"这个现象去反推。
            capabilities.append("llm:vision=none")
        # 网络隔离：报**实际生效**的机制，而不是配置里写的模式。
        # 两者必须分开暴露 —— 配置写 require 不等于隔离真的生效，
        # 而「以为隔离了其实没有」是这里最危险的误解。
        capabilities.append(f"sandbox:network={self.runner.isolator.network_mechanism()}")
        # 只读这里报的是**配置上打算用的机制**；逐次执行是否真的保护上了，
        # 由每次执行的 ExecResult.read_only_enforced/read_only_gaps 回答 ——
        # 某个挂载点重挂失败会让这一层失效，那是执行期才知道的。
        capabilities.append(f"sandbox:read_only={self.runner.isolator.read_only_mechanism()}")
        # 同上：报的是机制名（denylist/none），逐次是否真的装上由
        # ExecResult.seccomp_enforced 回答。
        capabilities.append(f"sandbox:seccomp={self.runner.isolator.seccomp_mechanism()}")

        checkpoint_backend = "memory"
        if self._runner is not None:
            checkpoint_backend = self._runner.checkpointer.backend

        return HealthStatus(
            healthy=True,
            version=__version__,
            # 报**实际生效**的 provider 与模型（可能来自服务商默认值，
            # 而不是配置里那个空串）。
            llm_provider="mock" if self.llm.is_mock else self.llm.text.provider,
            vlm_model=(
                self.llm.vision.model
                if self.llm.vision.supports_vision and self.llm.vision.usable
                else ""
            ),
            sandbox_ready=any(engines.values()),
            capabilities=capabilities,
            uptime_sec=int(time.time() - _STARTED_AT),
            toolchain=toolchain,
            engines=engines,
            checkpoint_backend=checkpoint_backend,
        )

    def renderer_availability(self) -> dict[str, bool]:
        """逐引擎可用性（供 /readyz 与启动日志使用）。"""
        return renderer_availability(self.settings)

    # ------------------------------------------------------------------
    # 导演：脚本 -> 分镜表
    # ------------------------------------------------------------------

    def plan_script(self, request: JobRequest) -> ScriptPlan:
        """执行导演智能体，返回结构化分镜表。"""
        logger.info(
            "收到剧本规划请求",
            extra={
                "job_id": request.job_id,
                "target_duration_sec": request.target_duration_sec,
                "locale": request.locale,
            },
        )
        return self.director.plan(
            job_id=request.job_id,
            raw_script=request.raw_script,
            style_guide=request.style_guide,
            target_duration_sec=request.target_duration_sec,
            locale=request.locale,
        )

    # ------------------------------------------------------------------
    # 完整流水线（服务端流式）
    # ------------------------------------------------------------------

    def run_pipeline(self, request: JobRequest) -> Iterator[dict[str, Any]]:
        """运行完整的 LangGraph 流水线，**逐条产出事件**。

        返回事件字典的可迭代对象，让 gRPC 流式响应能边产生边下发 ——
        先跑完整条流水线再一次性返回，会让前端在数十分钟里完全看不到进度。
        """
        yield from self.pipeline.run(request)

    # ------------------------------------------------------------------
    # 单镜头：编码 + 渲染
    # ------------------------------------------------------------------

    def generate_shot(
        self,
        *,
        job_id: str,
        shot: ShotSpec,
        attempt: int,
        style_guide: StyleGuide,
        feedback: CriticFeedback | None = None,
        output_dir: str = "",
        draft_only: bool = False,
        range_start_sec: float = 0.0,
        range_end_sec: float = 0.0,
    ) -> tuple[ShotSpec, RenderArtifact, bool]:
        """编码 + 渲染单个镜头（供 HITL 细化操作与调试使用）。

        返回 (分镜, 产物, 是否真的只渲染了请求的区间)。
        """
        engine = shot.engine.value if shot.engine else "stock"
        logger.info(
            "收到单镜头生成请求",
            extra={
                "job_id": job_id, "shot_id": shot.shot_id,
                "engine": engine, "attempt": attempt,
            },
        )

        result = self.coder.generate(
            shot=shot,
            style_guide=style_guide,
            attempt=attempt,
            feedback_text=_feedback_to_text(feedback),
            previous_code=shot.code,
        )
        if not result.policy_ok:
            # 静态检查失败也要如实返回：把危险/非法代码送去渲染，
            # 代价远高于直接失败（沙盒会拦，但会浪费一整轮渲染时间）。
            raise RenderFailed(f"生成的代码未通过安全/质量检查：{result.policy_summary}")

        updated = shot.model_copy(
            update={"code": result.code, "language": result.artifact.language}
        )
        if result.overlay_text:
            updated = updated.model_copy(
                update={"meta": {**updated.meta, "overlay_text": result.overlay_text}}
            )

        artifact, honored = self._render_shot(
            job_id=job_id, shot=updated, code=result.code, style_guide=style_guide,
            attempt=attempt, output_dir=output_dir, draft_only=draft_only,
            range_start_sec=range_start_sec, range_end_sec=range_end_sec,
        )
        return updated, artifact, honored

    # ------------------------------------------------------------------
    # 单镜头：审查
    # ------------------------------------------------------------------

    def critique_shot(
        self,
        *,
        job_id: str,
        shot: ShotSpec,
        artifact: RenderArtifact,
        attempt: int,
        style_guide: StyleGuide,
    ) -> CriticFeedback:
        """调用 VLM 审查渲染产物。"""
        logger.info(
            "收到单镜头审查请求",
            extra={
                "job_id": job_id, "shot_id": shot.shot_id,
                "frames": len(artifact.frame_samples),
            },
        )
        outcome = self.critic.review(
            shot=shot, artifact=artifact, style_guide=style_guide, attempt=attempt
        )
        return outcome.feedback

    # ------------------------------------------------------------------
    # 单镜头：人工意见回灌 + 重做
    # ------------------------------------------------------------------

    def revise_shot(
        self,
        *,
        job_id: str,
        shot: ShotSpec,
        human_comment: str,
        attempt: int,
        style_guide: StyleGuide,
        previous_error: str = "",
        previous_feedback: CriticFeedback | None = None,
        previous_artifact: RenderArtifact | None = None,
        output_dir: str = "",
        range_start_sec: float = 0.0,
        range_end_sec: float = 0.0,
    ) -> tuple[ShotSpec, RenderArtifact, CriticFeedback, bool]:
        """把人工意见转成可执行指令，重写代码、重新渲染并自动复审。

        普通意见回灌给模型；显式结构化修订首轮直接应用，仍通过
        同一套几何校验、浏览器预检、渲染和 VLM 复审。

        返回 (分镜, 产物, 复审反馈, 是否真的只渲染了请求的区间)。
        """
        logger.info(
            "收到镜头重做请求",
            extra={
                "job_id": job_id, "shot_id": shot.shot_id,
                "attempt": attempt, "comment": human_comment[:120],
            },
        )

        # Parse and preflight BEFORE the automatic repair loop. Invalid operator
        # edits fail closed rather than silently falling back to model generation.
        exact_revision = parse_exact_revision(human_comment)
        exact_result = None
        if exact_revision is not None:
            if range_end_sec > range_start_sec:
                raise ValueError("精确场景修订需要完整镜头复审，不支持局部时间区间")
            exact_result = self.coder.apply_exact_revision(
                shot=shot, style_guide=style_guide, revision=exact_revision)

        current = shot
        request_context = "用户期望：" + (human_comment.strip() or "根据系统诊断自动修复并复审")
        request_context += preserved_science(previous_feedback)
        if previous_error:
            request_context += "\n系统记录的失败原因：\n" + error_evidence("", previous_error, 2400)
        feedback_text = request_context + "\n" + _feedback_to_text(previous_feedback)
        last_valid = None
        previous_review = previous_feedback
        evidence_before = previous_artifact
        technical_failures = 0
        failure = "修订未产出可用视频"
        # Partial patches cannot be reviewed as complete scenes until Go splices
        # them. Full-shot edits get the same bounded repair opportunity as initial generation.
        rounds = 1 if range_end_sec > range_start_sec else min(3, self.settings.shot_max_attempts)
        for revision_round in range(rounds):
            round_attempt = attempt + revision_round
            logger.info("单镜修订轮次", extra={"shot_id": shot.shot_id, "round": revision_round+1, "limit": rounds})
            try:
                rebuild = (not current.code or technical_failures > 0 or
                           (rounds > 1 and revision_round == rounds - 1))
                generation_shot = current.model_copy(update={"meta": {
                    **current.meta, "repair_strategy": "rebuild" if rebuild else "targeted"}})
                science_context = [t for review in (previous_feedback, previous_review) if review
                                   for t in review.repair_tasks if t.task_id.startswith('science-')]
                if rebuild and science_context:
                    # A known-invalid director sketch must not keep winning over
                    # corrected scientific requirements in every regeneration.
                    generation_shot = generation_shot.model_copy(update={
                        "visual_brief": "依据旁白与反馈中的科学定义、限定条件重新设计图示；"
                                        "按验收目标选择正确机制图或有明确假设的曲线，重新安排布局。",
                        "beats": [],
                    })
                instructions = recovery_instruction(current, style_guide, rebuild=rebuild)
                result = exact_result if revision_round == 0 and exact_result is not None else self.coder.generate(
                    shot=generation_shot, style_guide=style_guide, attempt=round_attempt,
                    feedback_text=instructions + "\n" + feedback_text, previous_code=current.code,
                )
                if not result.policy_ok:
                    raise RenderFailed(f"重写后的代码未通过安全/质量检查：{result.policy_summary}")
                if current.code and result.code.strip() == current.code.strip():
                    raise RenderFailed("修订未改变实际代码，请重构反馈中的对象，不能只改说明")
                updated = current.model_copy(update={"code": result.code, "language": result.artifact.language})
                if result.overlay_text:
                    updated = updated.model_copy(update={"meta": {**updated.meta, "overlay_text": result.overlay_text}})
                # A failed render still has a concrete source to diagnose next time.
                current = updated
                artifact, honored = self._render_shot(
                    job_id=job_id, shot=updated, code=result.code, style_guide=style_guide,
                    attempt=round_attempt, output_dir=output_dir, draft_only=False,
                    range_start_sec=range_start_sec, range_end_sec=range_end_sec,
                    speech_path=last_valid[1].audio_path if last_valid else "",
                )
            except (RenderFailed, LLMError, ValueError) as exc:
                technical_failures += 1
                failure = str(exc)
                feedback_text += "\n本轮失败，必须修复后重新输出：" + failure[:2000]
                if isinstance(exc, RenderFailed) and not exc.retryable:
                    break
                continue

            rows = []
            if evidence_before and previous_review:
                before = evidence_before
                before_samples = _artifact_samples(before, self.settings.critic_frame_samples)
                after_samples = _artifact_samples(artifact, self.settings.critic_frame_samples)
                rows = precheck_repairs(previous_review, before, artifact,
                    [dict(path=s.path, ts=s.ts) for s in before_samples], after_samples,
                    Path(artifact.video_path).parent / "repair_evidence", self.runner)
                if skip_full_review(rows):
                    feedback_text += "\n对应问题的证据帧没有可见变化，请重构实际对象；跳过重复审核。"
                    continue
            outcome = self.critic.review(
                shot=updated, artifact=artifact, style_guide=style_guide,
                attempt=round_attempt, previous_feedback=feedback_text,
                previous_review=previous_review, repair_prechecks=rows,
                user_intent=human_comment,
            )
            last_valid = (updated, artifact, outcome.feedback, honored)
            if outcome.feedback.passed or outcome.degraded or not active_repairs(outcome.feedback):
                return last_valid
            current, previous_review = updated, outcome.feedback
            evidence_before = artifact
            feedback_text = request_context + preserved_science(previous_review) + "\n" + _feedback_to_text(previous_review)

        if last_valid:
            return last_valid
        raise RenderFailed(f"自动诊断修复在第 {revision_round + 1}/{rounds} 轮停止：{failure}。"
                           "未能生成通过门禁的新版本，已保留原产物与诊断；"
                           "仍可直接发起自动修复，无需填写技术修复方案。")

    # ------------------------------------------------------------------
    # 内部：渲染
    # ------------------------------------------------------------------

    def _render_shot(
        self,
        *,
        job_id: str,
        shot: ShotSpec,
        code: str,
        style_guide: StyleGuide,
        attempt: int,
        output_dir: str = "",
        draft_only: bool = False,
        range_start_sec: float = 0.0,
        range_end_sec: float = 0.0,
        speech_path: str = "",
    ) -> tuple[RenderArtifact, bool]:
        """渲染单个镜头并抽帧。

        抽帧与渲染绑定在一起，因为只有渲染现场才知道真实时长与产物路径 ——
        让调用方自己再抽一次会出现"用了错误时长导致抽到黑帧"的问题。

        返回 (产物, 是否真的只渲染了请求的区间)。后者必须如实上报：
        Go 侧据此决定把结果拼接回原片还是整镜替换。
        """
        engine = shot.engine.value if shot.engine else "stock"
        work_dir = (
            Path(output_dir)
            if output_dir
            else Path(self.settings.sandbox_work_dir) / job_id / f"shot_{shot.index:03d}"
        )
        # Render/TTS failures must never overwrite the currently published clip.
        work_dir = work_dir / f"render_{attempt}_{uuid.uuid4().hex[:10]}"

        request = RenderRequest(
            shot_id=shot.shot_id,
            code=code,
            output_dir=work_dir,
            duration_sec=shot.duration_sec,
            width=self.settings.render_width,
            height=self.settings.render_height,
            fps=self.settings.render_fps,
            draft=draft_only,
            overlay_text=shot.meta.get("overlay_text", ""),
            primary_color=style_guide.primary_color,
            background_color=style_guide.background_color,
            presenter_safe_area=style_guide.presenter_safe_area.model_dump() if style_guide.presenter_safe_area else None,
            range_start_sec=range_start_sec,
            range_end_sec=range_end_sec,
        )

        try:
            renderer = build_renderer(engine, self.settings)
            result = renderer.render(request, self.runner)
        except RendererError as exc:
            raise RenderFailed(error_evidence(f"渲染失败（{engine}）：{exc}", exc.detail),
                               retryable=exc.retryable) from exc

        frames: list[str] = []
        try:
            frames = extract_frames(
                result.video_path,
                work_dir / "frames",
                self.runner,
                count=self.settings.critic_frame_samples,
                duration_sec=result.duration_sec,
            )
        except MediaToolError as exc:
            # 抽帧失败不致命：审查环节会因为"无帧可用"而降级转人工，
            # 而不是伪造一个"通过"。
            logger.warning(
                "抽帧失败，审查将降级",
                extra={"shot_id": shot.shot_id, "error": str(exc)[:200]},
            )

        audio_path = speech_path if speech_path and Path(speech_path).is_file() else ""
        if not audio_path and not draft_only and not result.partial_range_honored and shot.narration.strip():
            provider = build_tts_provider(self.settings)
            if provider is not None:
                try:
                    speech = synthesize_with_retry(provider, shot.narration, out_path=work_dir.resolve()/"narration.mp3",
                        attempts=self.settings.tts_max_attempts, backoff_sec=self.settings.tts_retry_backoff_sec)
                    write_marks_sidecar(speech.audio_path, speech)
                    audio_path = str(speech.audio_path)
                except Exception as exc:
                    raise RenderFailed(f"重做配音失败，保留原片，请重试：{str(exc)[:300]}") from exc

        return RenderArtifact(
            artifact_id=f"{job_id}-{shot.shot_id}-{attempt}-{uuid.uuid4().hex[:6]}",
            shot_id=shot.shot_id,
            video_path=result.video_path,
            audio_path=audio_path,
            duration_sec=result.duration_sec,
            width=result.width,
            height=result.height,
            fps=int(result.fps) or self.settings.render_fps,
            attempt=attempt,
            engine=result.engine,
            frame_samples=frames,
            rendered_at_unix_ms=int(time.time() * 1000),
            render_cost_sec=round(result.render_cost_sec, 3),
        ), result.partial_range_honored


def _artifact_samples(artifact: RenderArtifact, count: int) -> list[FrameSample]:
    """Preserve skipped-frame indices; never zip a shortened list onto wrong times."""
    times = frame_sample_times(artifact.duration_sec, count)
    samples = []
    for path in artifact.frame_samples:
        stem = Path(path).stem
        if stem.startswith("frame_") and stem[6:].isdigit():
            index = int(stem[6:])
            if index < len(times):
                samples.append(FrameSample(path=path, ts=times[index]))
    return samples


def _feedback_to_text(feedback: CriticFeedback | None) -> str:
    """把结构化反馈渲染成回灌给编码智能体的文本。

    与 ``graph/nodes._collect_feedback`` 保持**同一套措辞**：
    人工意见与 VLM 意见格式一致，模型对两类输入的"处理方式"才一致。
    """
    if feedback is None:
        return ""
    from .review_tasks import format_repairs

    repairs = format_repairs(feedback)
    if repairs:
        return repairs
    parts: list[str] = []
    if feedback.issues:
        parts.append("【画面问题】\n" + "\n".join(f"- {i}" for i in feedback.issues[:6]))
    if feedback.suggestions:
        parts.append("【必须落实的修改】\n" + "\n".join(f"- {s}" for s in feedback.suggestions[:6]))
    return "\n\n".join(parts)
