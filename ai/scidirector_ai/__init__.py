"""SciDirector AI 大脑（多智能体科学视频导演）。

包结构约定：
    config      配置装载（环境变量）
    logging     结构化日志（与 Go 侧字段对齐）
    schemas     领域模型（Pydantic v2）
    llm         LLM / VLM 客户端封装（重试、超时、成本计量、mock）
    graph       LangGraph 状态与图拓扑
    agents      导演 / 编码 / 审查三个智能体
    sandbox     安全执行生成的渲染代码
    rag         Few-shot 优秀案例检索
    service     业务门面（HTTP 与 gRPC 共用）
    pb          protoc 生成代码（勿手改）
"""

__version__ = "0.1.0"
