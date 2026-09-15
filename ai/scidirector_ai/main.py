"""进程入口：在同一个进程内同时提供 FastAPI(HTTP) 与 gRPC 服务。

为什么双栈同进程？
* Go 层通过 gRPC 调用（强类型、支持流式）；
* 运维、调试与未来可能的前端直连走 HTTP（curl 友好、可挂 OpenAPI 文档）；
* 同进程意味着只有一份模型客户端与一份沙盒资源，避免内存翻倍与状态分裂。

生命周期：
    1. 装载配置、初始化日志；
    2. 构造 PipelineService 与 gRPC server；
    3. 启动 gRPC（后台线程）与 uvicorn（主线程）；
    4. 收到 SIGINT/SIGTERM 后**先停 gRPC 再停 HTTP**，给在途渲染留收尾窗口。
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import Settings, get_settings
from .grpc_server import build_server
from .llm import LLMError, LLMParseError
from .logging import bind_job, get_logger, setup_logging
from .schemas import JobRequest
from .service import PhaseNotImplemented, PipelineService, ServiceUnavailable

logger = get_logger(__name__)

# 进程级单例：在 lifespan 中构造，供所有请求复用。
_state: dict[str, object] = {}


# ===========================================================================
# HTTP 请求 / 响应模型
# ===========================================================================


class PlanRequest(BaseModel):
    """POST /v1/plan 的请求体（调试用：只跑导演智能体）。"""

    job_id: str = Field(default="debug-plan", description="任务标识，仅用于日志串联")
    raw_script: str = Field(min_length=1, description="科普脚本原文")
    target_duration_sec: float = Field(default=90.0, gt=0, le=3600)
    locale: str = "zh-CN"
    style_guide_json: str = Field(default="", description="风格约束的 JSON 字符串，可空")


class HealthResponse(BaseModel):
    status: str
    version: str
    uptime_sec: int
    llm_provider: str
    capabilities: list[str]
    toolchain: dict[str, bool]
    #: 逐引擎的工具链就绪度。单独成一个字段（而不是只塞进 capabilities 字符串）
    #: 是为了让监控系统能直接结构化消费，不必解析字符串。
    engines: dict[str, bool] = Field(default_factory=dict)


# ===========================================================================
# FastAPI 应用
# ===========================================================================


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """管理 gRPC server 与共享资源的生命周期。"""
    settings: Settings = app.state.settings
    service: PipelineService = _state["service"]  # type: ignore[assignment]

    grpc_server = build_server(settings, service)
    grpc_server.start()
    _state["grpc_server"] = grpc_server
    logger.info(
        "gRPC 服务已启动",
        extra={"addr": f"{settings.ai_grpc_host}:{settings.ai_grpc_port}"},
    )

    try:
        yield
    finally:
        # 先停 gRPC：它承载长任务，需要更长的收尾窗口。
        # grace=30s 让正在渲染的镜头把产物写完，避免留下半截 MP4。
        logger.info("正在停止 gRPC 服务（最长等待 30s）")
        stopped = grpc_server.stop(grace=30)
        # grpc 的 stop() 返回 Future；同步等待以保证进程不会提前退出。
        try:
            stopped.wait(timeout=35) if hasattr(stopped, "wait") else None
        except Exception:  # noqa: BLE001 - 收尾阶段的异常不应阻止退出
            logger.warning("等待 gRPC 停止超时，强制退出")
        logger.info("AI 大脑已退出")


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造 FastAPI 应用（工厂函数便于测试注入不同配置）。"""
    cfg = settings or get_settings()
    service = PipelineService(cfg)
    _state["service"] = service

    app = FastAPI(
        title="SciDirector AI Brain",
        version=__version__,
        description="多智能体科学视频导演：导演 / 编码 / 审查",
        lifespan=lifespan,
    )
    app.state.settings = cfg

    # ------------------------------------------------------------------
    # 中间件：trace_id 注入与结构化访问日志
    # ------------------------------------------------------------------
    @app.middleware("http")
    async def observability_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        from .logging import bind_trace

        trace_id = request.headers.get("X-Request-ID") or f"tr-{int(time.time() * 1000):x}"
        bind_trace(trace_id)
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - 交给异常处理器
            logger.exception("HTTP 请求异常", extra={"path": request.url.path})
            raise
        elapsed_ms = int((time.monotonic() - started) * 1000)
        response.headers["X-Request-ID"] = trace_id
        logger.info(
            "HTTP 访问",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "elapsed_ms": elapsed_ms,
            },
        )
        return response

    # ------------------------------------------------------------------
    # 异常处理：把领域异常映射为稳定的 HTTP 状态码
    # ------------------------------------------------------------------
    @app.exception_handler(PhaseNotImplemented)
    async def _phase_not_implemented(_: Request, exc: PhaseNotImplemented) -> JSONResponse:
        return JSONResponse(status_code=501, content={"error": "NOT_IMPLEMENTED", "message": str(exc)})

    @app.exception_handler(ServiceUnavailable)
    async def _unavailable(_: Request, exc: ServiceUnavailable) -> JSONResponse:
        return JSONResponse(status_code=503, content={"error": "UNAVAILABLE", "message": str(exc)})

    @app.exception_handler(LLMError)
    async def _llm_error(_: Request, exc: LLMError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"error": "LLM_ERROR", "message": str(exc)})

    @app.exception_handler(LLMParseError)
    async def _llm_parse_error(_: Request, exc: LLMParseError) -> JSONResponse:
        return JSONResponse(
            status_code=502, content={"error": "LLM_PARSE_ERROR", "message": str(exc)}
        )

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------
    @app.get("/healthz", response_model=HealthResponse, tags=["ops"])
    async def healthz() -> HealthResponse:
        """存活探针：进程能响应即返回 200。"""
        status = service.health()
        return HealthResponse(
            status="ok",
            version=status.version,
            uptime_sec=status.uptime_sec,
            llm_provider=status.llm_provider,
            capabilities=status.capabilities,
            toolchain=status.toolchain,
            engines=status.engines,
        )

    @app.get("/readyz", tags=["ops"])
    async def readyz() -> JSONResponse:
        """就绪探针：报告渲染引擎与工具链的就绪情况。

        与 /healthz 的区别：这里回答「能不能干活」，而不是「活着没」。
        所有渲染引擎的工具链都不可用时返回 503，让编排系统把流量摘走
        （此时服务确实一个镜头都产不出来）。

        注意 ``sandbox_ready`` 的含义是**工具链就绪度**，不是「渲染器已实现」——
        阶段一尚未实现任何渲染器，详见 docs/ROADMAP.md。
        """
        status = service.health()
        payload = {
            "status": "ok" if status.sandbox_ready else "degraded",
            "sandbox_ready": status.sandbox_ready,
            "engines": status.engines,
            "toolchain": status.toolchain,
            "capabilities": status.capabilities,
        }
        return JSONResponse(status_code=200 if status.sandbox_ready else 503, content=payload)

    @app.get("/version", tags=["ops"])
    async def version() -> dict[str, object]:
        return {"version": __version__, "settings": cfg.public_summary()}

    @app.post("/v1/plan", tags=["pipeline"])
    async def plan(request: PlanRequest) -> dict[str, object]:
        """只执行导演智能体：脚本 -> 分镜表。

        这是阶段一即可端到端跑通的能力，因此单独开放一个 HTTP 端点，
        方便在没有 Go 层的情况下用 curl 直接验证提示词与模型配置。

        LLM 调用是阻塞的，用 ``to_thread`` 挪到线程池，
        否则会卡住整个事件循环（表现为所有请求一起变慢）。
        """
        bind_job(request.job_id)
        job_request = JobRequest(
            job_id=request.job_id,
            raw_script=request.raw_script,
            target_duration_sec=request.target_duration_sec,
            locale=request.locale,
        )
        if request.style_guide_json:
            from .grpc_server import style_guide_from_json

            job_request.style_guide = style_guide_from_json(request.style_guide_json)

        plan_result = await asyncio.to_thread(service.plan_script, job_request)
        return {
            "job_id": request.job_id,
            "outline": plan_result.outline,
            "total_tokens": plan_result.total_tokens,
            "shots": [s.model_dump() for s in plan_result.shots],
        }

    @app.post("/v1/pipeline", tags=["pipeline"])
    async def run_pipeline(request: PlanRequest) -> dict[str, object]:
        """完整流水线（阶段二实现）。当前返回 501，明确告知而非假装成功。"""
        raise HTTPException(
            status_code=501,
            detail="阶段二实现：LangGraph 多智能体流水线；阶段一请使用 /v1/plan",
        )

    return app


# ===========================================================================
# CLI 入口
# ===========================================================================


def cli() -> int:
    """命令行入口：``python -m scidirector_ai.main`` 或安装后的 ``scidirector-ai``。"""
    import uvicorn

    settings = get_settings()
    setup_logging(settings.log_level, settings.service_name, json_output=not settings.is_dev)

    logger.info("SciDirector AI 大脑启动中", extra={"version": __version__, **settings.public_summary()})

    # 工具链预检：缺失会告警但不阻止启动 —— 只有对应标签的镜头会失败，
    # 而「整个服务起不来」会让所有任务都跑不了，那是更差的结果。
    toolchain = settings.toolchain_report()
    missing = [name for name, ok in toolchain.items() if not ok]
    if missing:
        logger.warning(
            "工具链不完整，相关标签的镜头将无法渲染",
            extra={"missing": missing, "toolchain": toolchain},
        )
    else:
        logger.info("工具链检查通过", extra={"toolchain": toolchain})

    app = create_app(settings)

    # 优雅退出：uvicorn 自己会处理 SIGINT/SIGTERM，这里只补充日志可观测性。
    def _on_signal(signum: int, _frame: object) -> None:
        logger.info("收到退出信号", extra={"signal": signal.Signals(signum).name})

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):  # pragma: no cover - 非主线程
            pass

    uvicorn.run(
        app,
        host=settings.ai_http_host,
        port=settings.ai_http_port,
        log_config=None,  # 交由本模块的 JSON logger 输出，避免两套日志格式
        access_log=False,  # 访问日志已在中间件中结构化输出
        timeout_graceful_shutdown=30,
    )
    return 0


if __name__ == "__main__":
    sys.exit(cli())
