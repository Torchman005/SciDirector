"""SciDirector AI 大脑（多智能体科学视频导演）。

包结构按**真实依赖方向**自下而上排列：

    config      配置装载（环境变量 + 校验）
    logging     结构化日志（字段与 Go 侧对齐）
    schemas     领域模型（Pydantic v2）
    pb          protoc 生成代码（勿手改）
    sandbox     安全执行层：policy(静态白名单) / runner(子进程+超时+内存限制)
                / manim(Manim 渲染沙盒)
    media       ffmpeg / ffprobe 原语（探测、抽帧、氛围镜头、帧序列编码）
    renderer    渲染调度：按「标签 -> 引擎」路由到具体实现
    llm         LLM / VLM 客户端（重试、结构化输出、成本计量、mock）
    rag         Few-shot 优秀案例检索
    agents      导演 / 编码 / 审查三个智能体
    graph       LangGraph 状态、节点、图装配与 checkpoint
    pbconv      proto <-> 领域模型转换
    service     业务门面（HTTP 与 gRPC 共用）
    grpc_server / main  传输层适配与进程入口

依赖方向是**单向**的（下层不引用上层）。这条约束不是洁癖：
早期把 media 放在 tools/ 包里、而 tools/__init__ 又导入上层的 renderer，
就形成过 tools -> renderer -> sandbox.manim -> tools.media 的循环导入。
"""

from __future__ import annotations

import os

__version__ = "0.2.0"


def _configure_tracing() -> None:
    """默认关闭 LangChain/LangSmith 的自动上报。

    为什么必须显式处理：
    langchain-core 会在**存在任何相关环境变量**时尝试把每一步的 trace
    上报到 smith.langchain.com。在离线/无密钥环境下，每一次图节点执行都会
    产生一条 401 报错并打印一大堆堆栈 —— 那会把真正有价值的业务日志淹没，
    也会给每个节点额外增加一次网络超时等待。

    本项目的可观测性方案是**自建的结构化日志 + 事件流**（字段与 Go 侧对齐，
    见 docs/DESIGN.md §8），不依赖外部 SaaS。需要 LangSmith 时显式设置
    ``SCID_LANGSMITH_TRACING=true`` 即可恢复。
    """
    if os.environ.get("SCID_LANGSMITH_TRACING", "").strip().lower() in ("1", "true", "yes", "on"):
        return
    for key in ("LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING", "LANGSMITH_TRACING"):
        os.environ[key] = "false"


_configure_tracing()
