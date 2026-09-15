"""gRPC 服务端：把 protobuf 契约适配到 :mod:`scidirector_ai.service`。

本模块承担三件事，且**只做这三件事**：
    1. 调用 :mod:`scidirector_ai.pbconv` 完成 proto <-> Pydantic 转换；
    2. 异常 -> gRPC status code 的映射；
    3. 链路字段（trace_id / job_id）的上下文绑定与解绑。

业务逻辑一律不写在这里 —— 否则 HTTP 与 gRPC 两条入口会逐渐漂移。
所有数据搬运都在 ``pbconv``，这样流水线节点也能复用同一套转换。

**状态码映射是契约的一部分**（见 Agent.md §9）：Go 侧依据状态码决定
「要不要重试」。映射错了不会报错，只会静默地把钱烧在无意义的重试上。
"""

from __future__ import annotations

import time
from concurrent import futures
from typing import Any, Iterator

import grpc

from .config import Settings, get_settings
from .llm import LLMError, LLMParseError
from .logging import bind_job, clear_bindings, get_logger
from .pb import _PB_ROOT  # noqa: F401 - 导入即完成 sys.path 注入，必须先于 pb 导入
from .pbconv import (
    artifact_from_pb,
    artifact_to_pb,
    event_to_pb,
    feedback_from_pb,
    feedback_to_pb,
    shot_from_pb,
    shot_to_pb,
    style_guide_from_json,
)
from .schemas import JobRequest
from .service import PipelineService, RenderFailed, ServiceUnavailable

from scidirector.v1 import ai_service_pb2 as pb  # type: ignore[import-not-found]
from scidirector.v1 import ai_service_pb2_grpc as pb_grpc  # type: ignore[import-not-found]
from scidirector.v1 import common_pb2 as common  # type: ignore[import-not-found]

logger = get_logger(__name__)


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

    def Health(self, request: Any, context: grpc.ServicerContext) -> Any:  # noqa: N802 - 方法名必须与 proto 一致
        status = self.service.health()
        capabilities = list(status.capabilities)
        # 把工具链明细也暴露出去：编排层据此判断哪些标签当前不可渲染，
        # 而不是等任务跑到那一步才失败。
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
                extra={
                    "job_id": job_id,
                    "target_duration_sec": request.target_duration_sec,
                    "max_attempts": request.max_attempts_per_shot,
                },
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
        except ServiceUnavailable as exc:
            context.abort(grpc.StatusCode.UNAVAILABLE, str(exc))
        except RenderFailed as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        except (LLMError, LLMParseError) as exc:
            context.abort(grpc.StatusCode.INTERNAL, f"模型调用失败：{exc}")
        except Exception as exc:  # noqa: BLE001 - 兜底，防止线程泄漏
            logger.exception("流水线异常")
            context.abort(grpc.StatusCode.INTERNAL, f"流水线内部错误：{exc}")
        finally:
            # 线程池会复用线程：不清理绑定，前一个请求的 job_id 会"泄漏"到
            # 后一个请求的日志里，这是极具误导性的一类 bug。
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
        except RenderFailed as exc:
            # FAILED_PRECONDITION：渲染失败（缺引擎/代码错误）重试没有意义，
            # 必须让 Go 侧判定为"不可重试"。
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        except ServiceUnavailable as exc:
            context.abort(grpc.StatusCode.UNAVAILABLE, str(exc))
        except (LLMError, LLMParseError) as exc:
            context.abort(grpc.StatusCode.INTERNAL, f"模型调用失败：{exc}")
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
            # degraded 表示"VLM 不可用，建议转人工"；它不影响状态码，
            # 因为调用本身是成功的（返回的是有效的降级结论）。
            degraded = not feedback.passed and str(feedback.source.value) == "SYSTEM"
            return pb.CritiqueShotResponse(
                feedback=feedback_to_pb(feedback),
                degraded=degraded,
                total_tokens=self.service.llm.usage.total_tokens,
                elapsed_sec=time.monotonic() - started,
            )
        except ServiceUnavailable as exc:
            context.abort(grpc.StatusCode.UNAVAILABLE, str(exc))
        except (LLMError, LLMParseError) as exc:
            context.abort(grpc.StatusCode.INTERNAL, f"模型调用失败：{exc}")
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
                # success 表示"重做并复审通过"；未通过也仍是成功的调用，
                # 由调用方依据 feedback.passed 决定下一步。
                success=bool(feedback.passed),
                total_tokens=self.service.llm.usage.total_tokens,
                elapsed_sec=time.monotonic() - started,
            )
        except RenderFailed as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        except ServiceUnavailable as exc:
            context.abort(grpc.StatusCode.UNAVAILABLE, str(exc))
        except (LLMError, LLMParseError) as exc:
            context.abort(grpc.StatusCode.INTERNAL, f"模型调用失败：{exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("ReviseShot 异常")
            context.abort(grpc.StatusCode.INTERNAL, f"镜头重做失败：{exc}")
        finally:
            clear_bindings()
        return None


# ===========================================================================
# 服务启动
# ===========================================================================


def build_server(
    settings: Settings | None = None, service: PipelineService | None = None
) -> grpc.Server:
    """构造（但尚未启动）gRPC 服务器。

    与 ``serve()`` 分开是为了让测试可以在随机端口上起一个真实 server
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
