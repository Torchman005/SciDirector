"""可观测性（阶段五）：追踪的开关语义与日志字段对齐。

**两条最需要钉住的行为**：

1. **没配 endpoint 时是无害的 no-op。** 本地不跑 collector 是常态；
   此时 `init_tracing` 必须返回 False、不报错、业务照常。返回布尔而不是 None，
   是为了让启动日志能**如实**说「追踪未启用」——「以为采到了、其实什么都没采」
   是这类集成最常见也最费时间的误解。
2. **日志里的 trace_id 与 OTel 的 trace ID 是同一个值。**
   没有这条，用日志里的 ID 去查链路必然查不到，而人只会以为「链路没采到」，
   不会怀疑是「两个都叫 trace_id 的东西」。
"""

from __future__ import annotations

import json
import logging
import sys

import pytest

from scidirector_ai import obs
from scidirector_ai.config import Settings
from scidirector_ai.logging import JsonFormatter, bind_trace, clear_bindings


@pytest.fixture(autouse=True)
def _reset() -> None:
    """每个用例都从「未初始化」开始。

    obs 模块把 provider 存在模块级变量里（进程级一次性初始化），
    不重置的话用例之间会互相影响：单个跑过、全量跑挂。
    """
    obs.shutdown_tracing()
    obs._initialized = False  # noqa: SLF001 - 本用例就是在测这个模块级状态
    clear_bindings()
    yield
    obs.shutdown_tracing()
    obs._initialized = False  # noqa: SLF001
    clear_bindings()


def _settings(**kw) -> Settings:
    base = {"otel_endpoint": "", "otel_service_name": "test-ai", "env": "test"}
    base.update(kw)
    return Settings(**base)


def test_init_without_endpoint_is_noop_and_says_so() -> None:
    """没配 endpoint：返回 False（未启用），且不抛异常。"""
    assert obs.init_tracing(_settings()) is False
    # 幂等：重复初始化不该报错，并且仍然如实返回 False。
    assert obs.init_tracing(_settings()) is False


def test_init_without_endpoint_still_allows_noop_tracer() -> None:
    """未启用时 tracer() 必须可用（no-op），让调用方不必到处写分支。"""
    obs.init_tracing(_settings())
    with obs.tracer().start_as_current_span("x"):
        pass  # 不应抛异常
    assert obs.current_trace_id() == ""


def test_log_formatter_uses_otel_trace_id() -> None:
    """**核心断言**：日志里的 trace_id 就是 OTel 的 trace ID。

    这条打通之后，「从日志跳到链路」才成立。用真实 SDK provider 产生 span，
    而不是打桩 —— 打桩会把「格式化成 32 位小写 hex」这一步一起假掉，
    而那恰恰是两侧能对上的关键（Tempo 里也是这个格式）。
    """
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.trace import set_tracer_provider

    set_tracer_provider(TracerProvider())
    fmt = JsonFormatter("test-ai")

    with obs.tracer().start_as_current_span("node"):
        record = logging.LogRecord(
            name="t", level=logging.INFO, pathname=__file__, lineno=1,
            msg="渲染完成", args=(), exc_info=None,
        )
        payload = json.loads(fmt.format(record))
        span_trace_id = obs.current_trace_id()

    assert span_trace_id, "活跃 span 应当有 trace ID"
    assert payload["trace_id"] == span_trace_id, (
        "日志里的 trace_id 必须等于 OTel 的 trace ID，否则「从日志查链路」会失效"
    )
    assert len(span_trace_id) == 32 and span_trace_id == span_trace_id.lower(), (
        "必须是 32 位小写 hex —— Tempo 里就是这个格式"
    )


def test_explicit_bind_trace_wins_over_otel() -> None:
    """显式绑定的 trace_id 优先于 span。

    HTTP 中间件在**没有** span 时也会绑一个 trace_id（保持既有行为）；
    显式绑定必须赢，否则那条路径上的日志会突然丢掉 trace_id。
    """
    fmt = JsonFormatter("test-ai")
    bind_trace("tr-explicit")
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="x", args=(), exc_info=None,
    )
    assert json.loads(fmt.format(record))["trace_id"] == "tr-explicit"


def test_log_formatter_survives_broken_otel() -> None:
    """可观测性坏掉时，日志**绝不能**跟着抛异常。

    这是日志路径上的一条硬约束：为了加一个追踪字段而让整个服务打不出日志，
    是把辅助能力做成了单点故障。
    """
    import builtins

    real_import = builtins.__import__

    def boom(name: str, *a, **kw):  # noqa: ANN001, ANN202
        if name.startswith("opentelemetry"):
            raise ImportError("模拟 opentelemetry 损坏")
        return real_import(name, *a, **kw)

    fmt = JsonFormatter("test-ai")
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="x", args=(), exc_info=None,
    )
    builtins.__import__ = boom
    try:
        payload = json.loads(fmt.format(record))
    finally:
        builtins.__import__ = real_import

    assert payload["msg"] == "x"
    assert "trace_id" not in payload, "取不到就如实不写，而不是写一个假值"


def test_current_trace_id_is_empty_without_span() -> None:
    assert obs.current_trace_id() == ""


def test_shutdown_is_safe_when_never_initialized() -> None:
    obs.shutdown_tracing()  # 不应抛异常
    obs.shutdown_tracing()


def test_init_soft_fails_when_packages_missing(monkeypatch) -> None:
    """配了端点却没装 opentelemetry：必须**软失败**（返回 False），不能崩。

    可观测性是辅助能力。配了 endpoint 就 ImportError 崩掉整个服务，
    等于把「加了个可选功能」变成「部署可能起不来」—— 而这条路径在
    只装核心依赖的环境里是必然会被走到的。
    """
    import builtins

    real_import = builtins.__import__

    def boom(name: str, *a, **kw):  # noqa: ANN001, ANN202
        if name.startswith("opentelemetry"):
            raise ImportError("模拟未安装 opentelemetry")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", boom)
    assert obs.init_tracing(_settings(otel_endpoint="127.0.0.1:4317")) is False
    # 未启用之后，tracer() 仍要是可用的 no-op。
    with obs.tracer().start_as_current_span("x"):
        pass
