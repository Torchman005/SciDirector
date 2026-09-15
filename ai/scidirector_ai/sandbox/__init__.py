"""沙盒：安全执行 LLM 生成的渲染代码。

分层防御（见 docs/DESIGN.md §5.4）：

    0. 静态白名单   ``policy.check_source``  —— AST 扫描，**进程启动前**拦截
    1. 进程隔离     ``runner.SandboxRunner`` —— 独立会话/进程组，超时杀整棵进程树
    2. 资源限制     同上                       —— 墙钟超时 + 内存 + CPU + 进程数
    3. 产物校验     ``manim.ManimSandbox``    —— 必须存在、非空、能被 ffprobe 解析
    4. 容器加固     生产环境                    —— --network=none --read-only 非 root

**必须清醒认识到：这是纵深防御，不是绝对安全边界。**
静态检查可以被绕过（例如拼接字符串构造危险名字）。
``SandboxRunner`` 会通过 ``ExecResult.memory_limit_enforced_by`` 如实上报
内存限制究竟由谁执行（rlimit / job-object / monitor / none）——
在缺少机制的环境里谎报"已限制"是安全代码里最危险的错误。

## 为什么 manim 子模块是**惰性**再导出的

``manim`` 依赖 ``scidirector_ai.media``（要跑 ffprobe 校验产物），
而 ``media`` 又依赖 ``sandbox.runner`` —— 一旦这里在导入期就把 ``manim``
拉进来，就会形成：

    media -> sandbox/__init__ -> manim -> media     （循环导入）

因此本模块只**急切**导入无外部依赖的 ``policy`` 与 ``runner``
（这两者只依赖标准库与 logging），``manim`` 相关符号通过 PEP 562 的
``__getattr__`` 按需加载。这样既没有循环，``from scidirector_ai.sandbox
import ManimSandbox`` 这种写法也依然可用。

顺带的好处：只想用 ``check_source`` 做静态检查的调用方，
不必为导入沙盒付出加载 ffmpeg 工具链的代价。
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from .policy import PolicyReport, PolicyViolation, check_source, is_safe
from .runner import (
    ExecResult,
    MemoryProbe,
    NullMemoryProbe,
    ResourceLimits,
    SandboxRunner,
)

#: 惰性再导出的符号 -> (子模块, 属性名)。
#: 新增惰性符号时必须同时补进 ``__all__``，否则静态检查工具会漏掉它。
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "DEFAULT_SCENE_CLASS": (".manim", "DEFAULT_SCENE_CLASS"),
    "ManimRenderRequest": (".manim", "ManimRenderRequest"),
    "ManimRenderResult": (".manim", "ManimRenderResult"),
    "ManimSandbox": (".manim", "ManimSandbox"),
    "ManimSandboxError": (".manim", "ManimSandboxError"),
    "extract_scene_class": (".manim", "extract_scene_class"),
    "newest_mp4": (".manim", "newest_mp4"),
}


def __getattr__(name: str) -> Any:
    """PEP 562：按需加载 manim 相关符号。

    加载后写回模块全局，后续访问不再走这里（``__getattr__`` 只在
    常规属性查找失败时才被调用）。
    """
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = target
    value = getattr(import_module(module_name, __name__), attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__))


__all__ = [
    "DEFAULT_SCENE_CLASS",
    "ExecResult",
    "ManimRenderRequest",
    "ManimRenderResult",
    "ManimSandbox",
    "ManimSandboxError",
    "MemoryProbe",
    "NullMemoryProbe",
    "PolicyReport",
    "PolicyViolation",
    "ResourceLimits",
    "SandboxRunner",
    "check_source",
    "extract_scene_class",
    "is_safe",
    "newest_mp4",
]
