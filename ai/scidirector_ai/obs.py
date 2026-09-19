"""可观测性：链路追踪（OpenTelemetry）的初始化与关闭。

## 为什么要有这个模块

Go 与 Python 是两个进程，中间隔着 gRPC。没有统一约定时，两侧各采各的，
「一次生成请求慢在哪」就只能靠对着两边日志的时间戳猜。拼成一棵树需要三件事：

1. **两侧用同一个传播规范**：W3C `traceparent`。Go 侧 otelgrpc 自动写进 gRPC metadata，
   本模块用全局 propagator 从 metadata 里解出来 —— 只要都用 W3C，就不需要任何自定义协议。
2. **两侧的 trace ID 与日志里的 trace_id 是同一个值**。见 `logging._current_otel_trace_id`：
   否则「拿日志里的 ID 查链路」必然查不到，而人只会以为链路没采到。
3. **未配置时必须是无害的 no-op**。本地不跑 collector 是常态，
   此时追踪全关、业务照常，而不是启动失败或刷一屏导出错误。

## 与 Go 侧的一处刻意差异

Go 侧自建了服务端 span（见 `httpapi.TraceMiddleware`），Python 侧则直接用
`GrpcInstrumentorServer`：Go 那边已有自己的 trace_id 语义要兼顾，
Python 这边没有历史包袱，让官方埋点接管能少写一层。

## span 的粒度选择

- gRPC 服务端：由 instrumentation 自动生成（含 `traceparent` 解出的父级）；
- 图节点：由 `graph/nodes.py` 显式打点。

**不为每个函数自动埋点**：链路的价值在于「一眼看出慢在哪一段」，
把成百上千个内部函数塞进一棵树只会淹没真正的瓶颈。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import logging as _stdlib_logging
from typing import Any

from .logging import get_logger

logger = get_logger(__name__)

#: 本服务的 tracer 名。Grafana/Tempo 里按它筛 Python 侧的 span。
TRACER_NAME = "scidirector-ai"

_initialized = False
_provider: Any = None


def _build_resource(settings: Any) -> Any:
    from opentelemetry.sdk.resources import Resource

    return Resource.create(
        {
            "service.name": settings.otel_service_name,
            "deployment.environment": settings.env,
        }
    )


def init_tracing(settings: Any) -> bool:
    """建立追踪；返回是否**真的**启用（而不是 no-op）。

    返回布尔而不是 None：调用方据此在启动日志里如实说明「追踪未启用」。
    「以为采到了、其实什么都没采」是这类集成最常见也最费时间的误解。
    """
    global _initialized, _provider

    if _initialized:
        return _provider is not None

    endpoint = (settings.otel_endpoint or "").strip()
    if not endpoint:
        _initialized = True
        return False

    # 配了端点但没装包时**必须软失败**：可观测性是辅助能力，
    # 它坏掉时正确的行为是「业务照常、日志里说明白」，而不是让整个服务起不来。
    # 这与「不配端点就是 no-op」是同一条原则的两面。
    try:
        import opentelemetry  # noqa: F401
    except ImportError as exc:
        logger.warning(
            "已配置 SCID_OTEL_ENDPOINT 但未安装 opentelemetry，链路追踪将不可用；"
            "安装：pip install -e '.[telemetry]'（或 pip install -r requirements.txt）",
            extra={"error": str(exc)},
        )
        _initialized = True
        return False

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.propagate import set_global_textmap
    from opentelemetry.propagators.composite import CompositePropagator
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    insecure = bool(settings.otel_insecure)
    exporter = OTLPSpanExporter(
        endpoint=endpoint,
        insecure=insecure,
        timeout=5,
    )
    provider = TracerProvider(resource=_build_resource(settings))
    # 批处理的间隔调小：本项目任务时长在秒级，默认 5s 会让「刚跑完就去查」查不到。
    provider.add_span_processor(BatchSpanProcessor(exporter, schedule_delay_millis=2000))
    trace.set_tracer_provider(provider)

    # **必须显式设置全局 propagator。** SDK 的默认值在多数版本里已是
    # TraceContext，但依赖「默认值恰好对」是危险的：一旦变了，
    # 表现是 Python 侧全都变成新的根 span —— 链路从中间断开，且没有任何报错。
    set_global_textmap(CompositePropagator([TraceContextTextMapPropagator()]))

    _provider = provider
    _initialized = True
    logger.info("链路追踪已启用", extra={"endpoint": endpoint, "service": settings.otel_service_name})
    return True


def instrument_grpc_server() -> bool:
    """给 gRPC 服务端装自动埋点（含从 metadata 解出 traceparent）。"""
    try:
        from opentelemetry.instrumentation.grpc import GrpcInstrumentorServer

        GrpcInstrumentorServer().instrument()
        return True
    except Exception as exc:  # noqa: BLE001 - 埋点失败不该让服务起不来
        logger.warning("gRPC 自动埋点失败，链路将缺少 Python 侧 span", extra={"error": str(exc)})
        return False


def instrument_fastapi(app: Any) -> bool:
    """给 FastAPI 装自动埋点。"""
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, tracer_provider=_provider)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("FastAPI 自动埋点失败", extra={"error": str(exc)})
        return False


def shutdown_tracing() -> None:
    """冲刷并关闭。

    **必须调用**：BatchSpanProcessor 会把最近的 span 留在内存里，
    不关就直接退出会丢掉最后几秒 —— 恰恰是崩溃现场最想看的那几秒。
    """
    global _provider
    if _provider is None:
        return
    try:
        _provider.shutdown()
    except Exception as exc:  # noqa: BLE001
        logger.warning("关闭追踪失败", extra={"error": str(exc)})
    finally:
        _provider = None


def current_trace_id() -> str:
    """当前 span 的 trace ID（hex）；无活跃 span 时返回空串。"""
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if not ctx.is_valid:
            return ""
        return format(ctx.trace_id, "032x")
    except Exception:  # noqa: BLE001
        return ""


class _NullSpan:
    """no-op span：吞掉所有属性设置。

    必须支持 `set_attribute` —— 节点装饰器会往里写 job_id/shot_index，
    少一个方法就会在「没装 opentelemetry」的环境里抛 AttributeError，
    而那时恰恰是最不该出问题的时候。
    """

    def set_attribute(self, *_a: Any, **_kw: Any) -> None:
        return None

    def set_status(self, *_a: Any, **_kw: Any) -> None:
        return None

    def record_exception(self, *_a: Any, **_kw: Any) -> None:
        return None


class _NullTracer:
    """no-op tracer：让调用方不必到处写 `if enabled` 分支。"""

    @contextmanager
    def start_as_current_span(self, *_a: Any, **_kw: Any) -> Iterator[_NullSpan]:
        yield _NullSpan()


def tracer() -> Any:
    """返回本服务的 tracer。

    未启用（或压根没装 opentelemetry）时返回 no-op tracer，调用方无需分支。
    这条「降级」很关键：图节点每次渲染都会调 `tracer()`，
    若它在这里抛 ImportError，那么「没装可观测性依赖」会变成「渲染全挂」——
    一个可选能力绝不该有这种杀伤力。
    """
    try:
        from opentelemetry import trace
    except ImportError:
        return _NullTracer()
    return trace.get_tracer(TRACER_NAME)


# 让 opentelemetry 内部的告警走标准 logging，避免它在没有 handler 时
# 直接打到 stderr 弄脏结构化日志。
_stdlib_logging.getLogger("opentelemetry").setLevel(_stdlib_logging.WARNING)
