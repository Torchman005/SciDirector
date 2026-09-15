"""gRPC 服务端：把 protobuf 契约适配到 :mod:`scidirector_ai.service`。

本模块承担三件事，且**只做这三件事**：
    1. proto <-> Pydantic 的双向转换（契约适配）；
    2. 异常 -> gRPC status code 的映射；
    3. 链路字段（trace_id / job_id）的上下文绑定与解绑。

业务逻辑一律不写在这里 —— 那样会让 HTTP 与 gRPC 两条入口逐渐漂移。
"""

from __future__ import annotations

import json
import time
from concurrent import futures
from typing import Any, Iterator

import grpc

from .config import Settings, get_settings
from .llm import LLMError, LLMParseError
from .logging import bind_job, clear_bindings, get_logger
from .pb import _PB_ROOT  # noqa: F401 - 导入即完成 sys.path 注入，必须在使用 pb 之前
from .schemas import (
    CriticFeedback,
    FeedbackSource,
    JobRequest,
    RenderArtifact,
    SceneTag,
    ShotSpec,
    StyleGuide,
)
from .service import PhaseNotImplemented, PipelineService, ServiceUnavailable

# 生成代码采用绝对导入（from scidirector.v1 import ...），
# 依赖 pb/__init__.py 中对 sys.path 的注入。
from scidirector.v1 import ai_service_pb2 as pb  # type: ignore[import-not-found]
from scidirector.v1 import ai_service_pb2_grpc as pb_grpc  # type: ignore[import-not-found]
from scidirector.v1 import common_pb2 as common  # type: ignore[import-not-found]

logger = get_logger(__name__)


# ===========================================================================
# 枚举映射
# ===========================================================================

_TAG_TO_PB: dict[SceneTag, int] = {
    SceneTag.MATH: common.SCENE_TAG_MATH,
    SceneTag.DATA: common.SCENE_TAG_DATA,
    SceneTag.CODE: common.SCENE_TAG_CODE,
    SceneTag.AMBIENCE: common.SCENE_TAG_AMBIENCE,
}
_PB_TO_TAG: dict[int, SceneTag] = {v: k for k, v in _TAG_TO_PB.items()}

_ENGINE_TO_PB: dict[str, int] = {
    "manim": common.RENDER_ENGINE_MANIM,
    "d3": common.RENDER_ENGINE_D3,
    "echarts": common.RENDER_ENGINE_ECHARTS,
    "code_anim": common.RENDER_ENGINE_CODE_ANIM,
    "stock": common.RENDER_ENGINE_STOCK,
}
_PB_TO_ENGINE: dict[int, str] = {v: k for k, v in _ENGINE_TO_PB.items()}

_STATUS_TO_PB: dict[str, int] = {
    "PENDING": common.SHOT_STATUS_PENDING,
    "GENERATING": common.SHOT_STATUS_GENERATING,
    "RENDERING": common.SHOT_STATUS_RENDERING,
    "CRITIQUING": common.SHOT_STATUS_CRITIQUING,
    "APPROVED": common.SHOT_STATUS_APPROVED,
    "REJECTED": common.SHOT_STATUS_REJECTED,
    "RETRYING": common.SHOT_STATUS_RETRYING,
    "FAILED": common.SHOT_STATUS_FAILED,
    "AWAITING_HUMAN": common.SHOT_STATUS_AWAITING_HUMAN,
}

_SOURCE_TO_PB: dict[FeedbackSource, int] = {
    FeedbackSource.VLM: common.FEEDBACK_SOURCE_VLM,
    FeedbackSource.HUMAN: common.FEEDBACK_SOURCE_HUMAN,
    FeedbackSource.SYSTEM: common.FEEDBACK_SOURCE_SYSTEM,
}


def tag_to_pb(tag: SceneTag) -> int:
    """场景标签 -> proto 枚举。未知标签降级为 UNSPECIFIED 而不是抛错。"""
    return _TAG_TO_PB.get(tag, common.SCENE_TAG_UNSPECIFIED)


def tag_from_pb(value: int) -> SceneTag:
    """proto 枚举 -> 场景标签。未识别时降级为 AMBIENCE（至少能产出占位画面）。"""
    return _PB_TO_TAG.get(value, SceneTag.AMBIENCE)


def engine_to_pb(engine: str | None) -> int:
    return _ENGINE_TO_PB.get(engine or "", common.RENDER_ENGINE_UNSPECIFIED)


def engine_from_pb(value: int) -> str:
    return _PB_TO_ENGINE.get(value, "")


# ===========================================================================
# 实体转换
# ===========================================================================


def shot_from_pb(message: Any) -> ShotSpec:
    """pb.ShotSpec -> 领域 ShotSpec。"""
    return ShotSpec(
        shot_id=message.shot_id,
        index=message.index,
        narration=message.narration,
        visual_brief=message.visual_brief,
        tag=tag_from_pb(message.tag),
        engine=engine_from_pb(message.engine) or None,
        duration_sec=message.duration_sec or 5.0,
        keywords=list(message.keywords),
        code=message.code,
        language=message.language,
        meta=dict(message.meta),
    )


def shot_to_pb(shot: ShotSpec) -> Any:
    """领域 ShotSpec -> pb.ShotSpec。"""
    return common.ShotSpec(
        shot_id=shot.shot_id,
        index=shot.index,
        narration=shot.narration,
        visual_brief=shot.visual_brief,
        tag=tag_to_pb(shot.tag),
        engine=engine_to_pb(shot.engine.value if shot.engine else None),
        duration_sec=shot.duration_sec,
        keywords=list(shot.keywords),
        code=shot.code,
        language=shot.language,
        meta=dict(shot.meta),
    )


def feedback_to_pb(feedback: CriticFeedback | None) -> Any:
    """领域 CriticFeedback -> pb.CriticFeedback。"""
    if feedback is None:
        return None
    return common.CriticFeedback(
        passed=feedback.passed,
        score=feedback.score,
        issues=list(feedback.issues),
        suggestions=list(feedback.suggestions),
        raw_response=feedback.raw_response,
        model=feedback.model,
        source=_SOURCE_TO_PB.get(feedback.source, common.FEEDBACK_SOURCE_UNSPECIFIED),
        attempt=feedback.attempt,
        logic_score=feedback.logic_score,
        readability_score=feedback.readability_score,
        pacing_score=feedback.pacing_score,
        aesthetics_score=feedback.aesthetics_score,
        created_at_unix_ms=int(time.time() * 1000),
    )


def feedback_from_pb(message: Any) -> CriticFeedback:
    """pb.CriticFeedback -> 领域 CriticFeedback。

    注意：proto 侧的 Feedback 允许 ``suggestions`` 为空（因为人类意见可能只有 issues），
    而领域模型强制「未通过必须给建议」。因此这里在转换时做一次补齐，
    而不是让 Pydantic 抛异常把整条链路打断。
    """
    if message is None:
        return CriticFeedback(passed=True, score=1.0)

    suggestions = list(message.suggestions)
    passed = bool(message.passed)
    if not passed and not suggestions:
        suggestions = list(message.issues) or ["请根据上述问题调整画面（字号、节奏、元素布局）"]

    source = {
        common.FEEDBACK_SOURCE_HUMAN: FeedbackSource.HUMAN,
        common.FEEDBACK_SOURCE_SYSTEM: FeedbackSource.SYSTEM,
    }.get(message.source, FeedbackSource.VLM)

    return CriticFeedback(
        passed=passed,
        score=float(message.score),
        issues=list(message.issues),
        suggestions=suggestions,
        raw_response=message.raw_response,
        model=message.model,
        source=source,
        attempt=int(message.attempt),
        logic_score=float(message.logic_score),
        readability_score=float(message.readability_score),
        pacing_score=float(message.pacing_score),
        aesthetics_score=float(message.aesthetics_score),
    )


def artifact_to_pb(artifact: RenderArtifact | None) -> Any:
    """领域 RenderArtifact -> pb.RenderArtifact。"""
    if artifact is None:
        return None
    return common.RenderArtifact(
        artifact_id=artifact.artifact_id,
        shot_id=artifact.shot_id,
        video_path=artifact.video_path,
        audio_path=artifact.audio_path,
        subtitle_path=artifact.subtitle_path,
        duration_sec=artifact.duration_sec,
        width=artifact.width,
        height=artifact.height,
        fps=artifact.fps,
        attempt=artifact.attempt,
        engine=artifact.engine,
        frame_samples=list(artifact.frame_samples),
        rendered_at_unix_ms=artifact.rendered_at_unix_ms,
        render_cost_sec=artifact.render_cost_sec,
    )


def artifact_from_pb(message: Any) -> RenderArtifact:
    """pb.RenderArtifact -> 领域 RenderArtifact。"""
    if message is None:
        return RenderArtifact()
    return RenderArtifact(
        artifact_id=message.artifact_id,
        shot_id=message.shot_id,
        video_path=message.video_path,
        audio_path=message.audio_path,
        subtitle_path=message.subtitle_path,
        duration_sec=message.duration_sec,
        width=message.width,
        height=message.height,
        fps=message.fps,
        attempt=message.attempt,
        engine=message.engine,
        frame_samples=list(message.frame_samples),
        rendered_at_unix_ms=message.rendered_at_unix_ms,
        render_cost_sec=message.render_cost_sec,
    )


def style_guide_from_json(raw: str) -> StyleGuide:
    """把 Go 侧传来的 style_guide_json 解析为 StyleGuide。

    解析失败时**降级为默认风格**而不是让请求失败：
    风格是锦上添花，不该因为一个坏的 JSON 把整次生成打掉。
    """
    if not raw or not raw.strip():
        return StyleGuide()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("style_guide_json 解析失败，使用默认风格", extra={"raw_prefix": raw[:200]})
        return StyleGuide()
    if not isinstance(payload, dict):
        return StyleGuide()
    try:
        return StyleGuide.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - 校验失败一律降级
        logger.warning("style_guide 校验失败，使用默认风格", extra={"error": str(exc)[:300]})
        return StyleGuide()


def event_to_pb(event: dict[str, Any]) -> Any:
    """内部事件字典 -> pb.PipelineEvent（流式响应的单条消息）。"""
    return common.PipelineEvent(
        job_id=event.get("job_id", ""),
        shot_id=event.get("shot_id", "") or "",
        node=event.get("node", "") or "",
        status=_STATUS_TO_PB.get(event.get("status", ""), common.SHOT_STATUS_UNSPECIFIED),
        message=event.get("message", "") or "",
        attempt=int(event.get("attempt", 0) or 0),
        shot_index=int(event.get("shot_index", 0) or 0),
        total_shots=int(event.get("total_shots", 0) or 0),
        progress=float(event.get("progress", 0.0) or 0.0),
        artifact=artifact_to_pb(event.get("artifact")),
        feedback=feedback_to_pb(event.get("feedback")),
        error=event.get("error", "") or "",
        ts_unix_ms=int(event.get("ts_unix_ms", 0) or 0) or int(time.time() * 1000),
        payload_json=event.get("payload_json", "") or "",
    )


# ===========================================================================
# Servicer
# ===========================================================================


class AiDirectorServicer(pb_grpc.AiDirectorServiceServicer):
    """AiDirectorService 的实现。

    并发模型：gRPC 的 ``ThreadPoolExecutor`` 会并发调用这些方法。
    渲染是阻塞的，因此线程数（``grpc_max_workers``）需要显著大于 CPU 核数。
    """

    def __init__(self, service: PipelineService) -> None:
        self.service = service

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def Health(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802 - gRPC 方法名必须与 proto 一致
        status = self.service.health()
        capabilities = list(status.capabilities)
        # 把工具链明细也暴露出去：编排层据此判断哪些标签当前不可渲染。
        for tool, ok in status.toolchain.items():
            capabilities.append(f"tool:{tool}={'ok' if ok else 'missing'}")

        return common.HealthResponse(
            healthy=status.healthy,
            version=status.version,
            llm_provider=status.llm_provider,
            vlm_model=status.vlm_model,
            sandbox_ready=status.sandbox_ready,
            capabilities=capabilities,
            uptime_sec=status.uptime_sec,
        )

    # ------------------------------------------------------------------
    # RunPipeline（服务端流式）
    # ------------------------------------------------------------------

    def RunPipeline(self, request: Any, context: grpc.ServicerContext) -> Iterator[Any]:  # noqa: N802
        job_id = request.job_id
        bind_job(job_id)
        try:
            logger.info(
                "收到流水线请求",
                extra={"job_id": job_id, "target_duration_sec": request.target_duration_sec},
            )
            job_request = JobRequest(
                job_id=job_id,
                raw_script=request.raw_script,
                style_guide=style_guide_from_json(request.style_guide_json),
                target_duration_sec=request.target_duration_sec or 90.0,
                max_attempts_per_shot=request.max_attempts_per_shot or 3,
                locale=request.locale or "zh-CN",
                resume=request.resume,
                checkpoint_thread_id=request.checkpoint_thread_id or job_id,
            )
            for event in self.service.run_pipeline(job_request):
                if not context.is_active():
                    # 客户端已断开（例如人工取消）：立刻停止，避免继续烧渲染资源。
                    logger.info("客户端已断开，终止流水线", extra={"job_id": job_id})
                    return
                yield event_to_pb(event)
        except PhaseNotImplemented as exc:
            context.abort(grpc.StatusCode.UNIMPLEMENTED, str(exc))
        except ServiceUnavailable as exc:
            context.abort(grpc.StatusCode.UNAVAILABLE, str(exc))
        except (LLMError, LLMParseError) as exc:
            context.abort(grpc.StatusCode.INTERNAL, f"模型调用失败：{exc}")
        except Exception as exc:  # noqa: BLE001 - 兜底，防止线程泄漏
            logger.exception("流水线异常")
            context.abort(grpc.StatusCode.INTERNAL, f"流水线内部错误：{exc}")
        finally:
            clear_bindings()

    # ------------------------------------------------------------------
    # PlanScript
    # ------------------------------------------------------------------

    def PlanScript(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        job_id = request.job_id
        bind_job(job_id)
        started = time.monotonic()
        try:
            job_request = JobRequest(
                job_id=job_id,
                raw_script=request.raw_script,
                style_guide=style_guide_from_json(request.style_guide_json),
                target_duration_sec=request.target_duration_sec or 90.0,
                locale=request.locale or "zh-CN",
            )
            plan = self.service.plan_script(job_request)
            return pb.PlanScriptResponse(
                shots=[shot_to_pb(s) for s in plan.shots],
                outline=plan.outline,
                total_tokens=plan.total_tokens,
                elapsed_sec=time.monotonic() - started,
            )
        except (LLMError, LLMParseError) as exc:
            logger.warning("剧本规划失败", extra={"job_id": job_id, "error": str(exc)})
            context.abort(grpc.StatusCode.INTERNAL, f"导演智能体失败：{exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("PlanScript 异常")
            context.abort(grpc.StatusCode.INTERNAL, f"内部错误：{exc}")
        finally:
            clear_bindings()
        return None  # 不可达，仅为满足类型检查

    # ------------------------------------------------------------------
    # GenerateShot
    # ------------------------------------------------------------------

    def GenerateShot(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        bind_job(request.job_id)
        started = time.monotonic()
        try:
            shot = shot_from_pb(request.shot)
            feedback = feedback_from_pb(request.feedback) if request.HasField("feedback") else None
            new_shot, artifact = self.service.generate_shot(
                job_id=request.job_id,
                shot=shot,
                attempt=request.attempt,
                style_guide=style_guide_from_json(request.style_guide_json),
                feedback=feedback,
                output_dir=request.output_dir,
                draft_only=request.draft_only,
            )
            return pb.GenerateShotResponse(
                shot=shot_to_pb(new_shot),
                artifact=artifact_to_pb(artifact),
                success=True,
                total_tokens=self.service.llm.usage.total_tokens,
                elapsed_sec=time.monotonic() - started,
            )
        except PhaseNotImplemented as exc:
            context.abort(grpc.StatusCode.UNIMPLEMENTED, str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("GenerateShot 异常")
            context.abort(grpc.StatusCode.INTERNAL, f"镜头生成失败：{exc}")
        finally:
            clear_bindings()
        return None

    # ------------------------------------------------------------------
    # CritiqueShot
    # ------------------------------------------------------------------

    def CritiqueShot(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        bind_job(request.job_id)
        started = time.monotonic()
        try:
            feedback = self.service.critique_shot(
                job_id=request.job_id,
                shot=shot_from_pb(request.shot),
                artifact=artifact_from_pb(request.artifact),
                attempt=request.attempt,
                style_guide=style_guide_from_json(request.style_guide_json),
            )
            return pb.CritiqueShotResponse(
                feedback=feedback_to_pb(feedback),
                degraded=False,
                total_tokens=self.service.llm.usage.total_tokens,
                elapsed_sec=time.monotonic() - started,
            )
        except PhaseNotImplemented as exc:
            context.abort(grpc.StatusCode.UNIMPLEMENTED, str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("CritiqueShot 异常")
            context.abort(grpc.StatusCode.INTERNAL, f"审查失败：{exc}")
        finally:
            clear_bindings()
        return None

    # ------------------------------------------------------------------
    # ReviseShot
    # ------------------------------------------------------------------

    def ReviseShot(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802
        bind_job(request.job_id)
        started = time.monotonic()
        try:
            shot, artifact, feedback = self.service.revise_shot(
                job_id=request.job_id,
                shot=shot_from_pb(request.shot),
                human_comment=request.human_comment,
                attempt=request.attempt,
                style_guide=style_guide_from_json(request.style_guide_json),
                output_dir=request.output_dir,
            )
            return pb.ReviseShotResponse(
                shot=shot_to_pb(shot),
                artifact=artifact_to_pb(artifact),
                feedback=feedback_to_pb(feedback),
                success=True,
                total_tokens=self.service.llm.usage.total_tokens,
                elapsed_sec=time.monotonic() - started,
            )
        except PhaseNotImplemented as exc:
            context.abort(grpc.StatusCode.UNIMPLEMENTED, str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("ReviseShot 异常")
            context.abort(grpc.StatusCode.INTERNAL, f"镜头重做失败：{exc}")
        finally:
            clear_bindings()
        return None


# ===========================================================================
# 服务启动
# ===========================================================================


def build_server(settings: Settings | None = None, service: PipelineService | None = None) -> grpc.Server:
    """构造（但尚未启动）gRPC 服务器。

    与 ``serve()`` 分开是为了让测试可以直接在随机端口上起一个真实 server
    做端到端校验，而不必启动整个进程。
    """
    cfg = settings or get_settings()
    svc = service or PipelineService(cfg)

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=cfg.grpc_max_workers),
        options=[
            # 单项消息上限放宽：审查 RPC 会携带多张抽帧的元数据（路径列表）。
            ("grpc.max_receive_message_length", 32 * 1024 * 1024),
            ("grpc.max_send_message_length", 32 * 1024 * 1024),
            # 长任务场景：保持连接活跃，避免被中间设备静默断开。
            ("grpc.keepalive_time_ms", 30_000),
            ("grpc.keepalive_timeout_ms", 10_000),
            ("grpc.keepalive_permit_without_calls", 1),
        ],
    )
    pb_grpc.add_AiDirectorServiceServicer_to_server(AiDirectorServicer(svc), server)

    # 标准 gRPC 健康检查：k8s 的 grpc 探针可直接使用，无需自研客户端。
    try:
        from grpc_health.v1 import health, health_pb2, health_pb2_grpc

        health_servicer = health.HealthServicer()
        # 空字符串代表整个 server 的总体健康状态（gRPC 健康检查协议约定）。
        health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)
        health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    except ImportError:  # 可选依赖，缺失不影响主服务
        logger.debug("grpc_health 未安装，跳过标准健康检查服务")

    address = f"{cfg.ai_grpc_host}:{cfg.ai_grpc_port}"
    server.add_insecure_port(address)
    return server
