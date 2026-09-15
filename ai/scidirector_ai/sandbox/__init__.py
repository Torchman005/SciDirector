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
"""

from .manim import (
    DEFAULT_SCENE_CLASS,
    ManimRenderRequest,
    ManimRenderResult,
    ManimSandbox,
    ManimSandboxError,
    extract_scene_class,
    newest_mp4,
)
from .policy import PolicyReport, PolicyViolation, check_source, is_safe
from .runner import (
    ExecResult,
    MemoryProbe,
    NullMemoryProbe,
    ResourceLimits,
    SandboxRunner,
)

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
