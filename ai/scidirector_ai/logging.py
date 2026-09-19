"""结构化日志。

目标：让 Python 与 Go 两侧的日志字段**完全对齐**，这样在同一个采集管道里
可以直接按 ``job_id`` 把整条链路（HTTP -> 队列 -> gRPC -> 渲染 -> 审查）串起来。

字段约定（与 ``backend/internal/logging`` 保持一致）：
    trace_id / job_id / shot_id / attempt / node / task_id

实现选择：直接用标准库 ``logging`` + 自定义 JSON Formatter，
而不引入 structlog 作为硬依赖 —— 少一个依赖就少一个版本冲突面，
而这里需要的「结构化 + contextvar 绑定」标准库完全够用。
"""

from __future__ import annotations

import json
import logging
import sys
import time
from contextvars import ContextVar
from typing import Any

# ---------------------------------------------------------------------------
# 上下文绑定：用 ContextVar 而非 threading.local
# 因为 gRPC 的同步 handler 跑在线程池里，而 asyncio 任务跑在事件循环中，
# ContextVar 在两种模型下都能正确隔离（asyncio 尤其重要）。
# ---------------------------------------------------------------------------
_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)
_job_id: ContextVar[str | None] = ContextVar("job_id", default=None)

# 与 Go 侧一致的字段名常量，避免各处拼字符串写错。
FIELD_TRACE_ID = "trace_id"
FIELD_JOB_ID = "job_id"
FIELD_SHOT_ID = "shot_id"
FIELD_ATTEMPT = "attempt"
FIELD_NODE = "node"

_RESERVED = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
}



def _current_otel_trace_id() -> str | None:
    """当前 OpenTelemetry span 的 trace ID（hex）；无活跃 span 时返回 None。

    **惰性导入**：opentelemetry 是可选的（未配置追踪时不必装），
    而且这里在每条日志的渲染路径上 —— 顶层导入会把 opentelemetry 的
    导入开销强加给所有使用者（包括只跑单测的场景）。
    """
    try:
        from opentelemetry import trace as _otel_trace

        ctx = _otel_trace.get_current_span().get_span_context()
        if not ctx.is_valid:
            return None
        return format(ctx.trace_id, "032x")
    except Exception:  # noqa: BLE001 - 日志路径绝不能因为可观测性而抛异常
        return None


class JsonFormatter(logging.Formatter):
    """把一条日志渲染为单行 JSON。

    单行的原因很实际：多行 JSON 在容器日志采集（fluent-bit / Loki）里会被
    切成多条记录，堆栈信息也会散落，排查体验极差。
    """

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003 - 覆写标准库方法名
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "service": self.service,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # 自动附加 contextvar 中的链路字段：调用方无需每处都手动传。
        #
        # 回退到 OpenTelemetry 当前 span 的 trace ID：这是让「日志里的 trace_id」
        # 与「Tempo 里的 trace ID」保持**同一个值**的关键一步。
        # 没有这一步时，两边各是一个 ID —— 而用日志里的 ID 去查链路正是最常用的
        # 排查动作，查不到时人只会以为「链路没采到」，不会怀疑是 ID 不一致。
        tid = _trace_id.get() or _current_otel_trace_id()
        if tid is not None:
            payload[FIELD_TRACE_ID] = tid
        if (jid := _job_id.get()) is not None:
            payload[FIELD_JOB_ID] = jid

        # 合并调用方通过 extra= 传入的自定义字段。
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = _safe(value)

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def _safe(value: Any) -> Any:
    """把不可 JSON 序列化的值转成字符串，避免因为一个日志字段把请求打挂。"""
    if isinstance(value, (str, int, float, bool, type(None), list, dict)):
        return value
    return repr(value)


def setup_logging(level: str = "info", service: str = "scid-ai", *, json_output: bool = True) -> None:
    """初始化根 logger。应在进程启动最早期调用一次。"""
    root = logging.getLogger()
    # 幂等：重复调用时先清空 handler，避免日志重复输出。
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    if json_output:
        handler.setFormatter(JsonFormatter(service))
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
        )
    root.addHandler(handler)
    root.setLevel(level.upper())

    # 压低第三方库的噪声：这些库在 debug 级别会输出大量无意义的帧信息。
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "grpc"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """获取一个 logger。约定所有模块用 ``get_logger(__name__)``。"""
    return logging.getLogger(name)


def bind_job(job_id: str, trace_id: str | None = None) -> None:
    """把 job_id（可选 trace_id）绑定到当前上下文。

    gRPC handler 在入口调用一次，之后该请求内所有日志自动带上这两个字段。
    """
    _job_id.set(job_id)
    if trace_id:
        _trace_id.set(trace_id)


def bind_trace(trace_id: str) -> None:
    """只绑定 trace_id（HTTP 中间件用）。"""
    _trace_id.set(trace_id)


def clear_bindings() -> None:
    """清空上下文绑定。

    线程池复用线程时必须调用：否则前一个请求的 job_id 会「泄漏」到后一个请求的日志里，
    这是排查线上问题时极具误导性的一类 bug。
    """
    _job_id.set(None)
    _trace_id.set(None)
