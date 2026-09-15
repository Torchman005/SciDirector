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
from .graph.builder import PipelineRunner
from .llm import LLMClient
from .logging import get_logger
from .media import MediaToolError, extract_frames
from .renderer import RendererError, RenderRequest, build_renderer, renderer_availability
from .sandbox.runner import SandboxRunner
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
            self._sandbox = SandboxRunner()
        return self._sandbox

    @property
    def pipeline(self) -> PipelineRunner:
        if self._runner is None:
            self._runner = PipelineRunner(self.settings, llm=self.llm)
        return self._runner

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
        if any(engines.values()):
            capabilities.append("render")
        if toolchain.get("ffmpeg"):
            capabilities.append("compose")
        for name, ok in sorted(engines.items()):
            capabilities.append(f"engine:{name}={'ok' if ok else 'missing'}")
        # "pipeline" 表示 run_pipeline 已实现 —— 冒烟脚本据此自动切换
        # 「阶段一预期 UNIMPLEMENTED」与「阶段二预期真实结果」。
        capabilities.append("pipeline")
        capabilities.append("mock-llm" if self.llm.is_mock else "vlm")

        checkpoint_backend = "memory"
        if self._runner is not None:
            checkpoint_backend = self._runner.checkpointer.backend

        return HealthStatus(
            healthy=True,
            version=__version__,
            llm_provider="mock" if self.llm.is_mock else self.settings.llm_provider,
            vlm_model=self.settings.vlm_model,
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
    ) -> tuple[ShotSpec, RenderArtifact]:
        """编码 + 渲染单个镜头（供 HITL 细化操作与调试使用）。"""
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
            raise RenderFailed(f"生成的代码未通过静态安全检查：{result.policy_summary}")

        updated = shot.model_copy(
            update={"code": result.code, "language": result.artifact.language}
        )
        if result.overlay_text:
            updated = updated.model_copy(
                update={"meta": {**updated.meta, "overlay_text": result.overlay_text}}
            )

        artifact = self._render_shot(
            job_id=job_id, shot=updated, code=result.code, style_guide=style_guide,
            attempt=attempt, output_dir=output_dir, draft_only=draft_only,
        )
        return updated, artifact

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
        output_dir: str = "",
    ) -> tuple[ShotSpec, RenderArtifact, CriticFeedback]:
        """把人工意见转成可执行指令，重写代码、重新渲染并自动复审。

        人工意见与 VLM 意见走**完全相同的通道**（都变成一段回灌文本），
        因此这里只需把 ``human_comment`` 当作 feedback 传下去 ——
        编码侧无需知道意见来自人还是模型。
        """
        logger.info(
            "收到镜头重做请求",
            extra={
                "job_id": job_id, "shot_id": shot.shot_id,
                "attempt": attempt, "comment": human_comment[:120],
            },
        )

        human_feedback = CriticFeedback(
            passed=False,
            score=0.0,
            issues=[human_comment],
            suggestions=[human_comment],
            source=FeedbackSource.HUMAN,
            attempt=attempt,
        )

        result = self.coder.generate(
            shot=shot,
            style_guide=style_guide,
            attempt=attempt,
            feedback_text=_feedback_to_text(human_feedback),
            previous_code=shot.code,
        )
        if not result.policy_ok:
            raise RenderFailed(f"重写后的代码未通过静态安全检查：{result.policy_summary}")

        updated = shot.model_copy(
            update={"code": result.code, "language": result.artifact.language}
        )
        if result.overlay_text:
            updated = updated.model_copy(
                update={"meta": {**updated.meta, "overlay_text": result.overlay_text}}
            )

        artifact = self._render_shot(
            job_id=job_id, shot=updated, code=result.code, style_guide=style_guide,
            attempt=attempt, output_dir=output_dir, draft_only=False,
        )

        # 重做后自动复审：让审核台立刻知道"改好了没有"，
        # 而不是让人自己盯着预览视频判断。
        outcome = self.critic.review(
            shot=updated, artifact=artifact, style_guide=style_guide,
            attempt=attempt, previous_feedback=human_comment,
        )
        return updated, artifact, outcome.feedback

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
    ) -> RenderArtifact:
        """渲染单个镜头并抽帧。

        抽帧与渲染绑定在一起，因为只有渲染现场才知道真实时长与产物路径 ——
        让调用方自己再抽一次会出现"用了错误时长导致抽到黑帧"的问题。
        """
        engine = shot.engine.value if shot.engine else "stock"
        work_dir = (
            Path(output_dir)
            if output_dir
            else Path(self.settings.sandbox_work_dir) / job_id / f"shot_{shot.index:03d}"
        )

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
        )

        try:
            renderer = build_renderer(engine, self.settings)
            result = renderer.render(request, self.runner)
        except RendererError as exc:
            raise RenderFailed(f"渲染失败（{engine}）：{exc}") from exc

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

        return RenderArtifact(
            artifact_id=f"{job_id}-{shot.shot_id}-{attempt}-{uuid.uuid4().hex[:6]}",
            shot_id=shot.shot_id,
            video_path=result.video_path,
            duration_sec=result.duration_sec,
            width=result.width,
            height=result.height,
            fps=int(result.fps) or self.settings.render_fps,
            attempt=attempt,
            engine=result.engine,
            frame_samples=frames,
            rendered_at_unix_ms=int(time.time() * 1000),
            render_cost_sec=round(result.render_cost_sec, 3),
        )


def _feedback_to_text(feedback: CriticFeedback | None) -> str:
    """把结构化反馈渲染成回灌给编码智能体的文本。

    与 ``graph/nodes._collect_feedback`` 保持**同一套措辞**：
    人工意见与 VLM 意见格式一致，模型对两类输入的"处理方式"才一致。
    """
    if feedback is None:
        return ""
    parts: list[str] = []
    if feedback.issues:
        parts.append("【画面问题】\n" + "\n".join(f"- {i}" for i in feedback.issues[:6]))
    if feedback.suggestions:
        parts.append("【必须落实的修改】\n" + "\n".join(f"- {s}" for s in feedback.suggestions[:6]))
    return "\n\n".join(parts)
