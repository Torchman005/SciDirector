"""业务门面：HTTP 与 gRPC 两条入口共用同一套逻辑。

为什么要有这一层？
* 避免 HTTP handler 与 gRPC servicer 各写一遍业务逻辑（那必然导致行为漂移）；
* 让业务逻辑可以在没有网络的情况下被单测直接调用；
* 传输层的关注点（proto 转换、状态码、tracing）被隔离在各自的适配器里。

阶段一边界：
    health / plan_script 已实现；
    run_pipeline / generate_shot / critique_shot / revise_shot 在阶段二实现，
    当前显式抛出 :class:`PhaseNotImplemented`（而不是返回假数据）——
    「静默返回空结果」会让 Go 侧以为任务成功，是最糟糕的失败形态。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterator

from . import __version__
from .agents.director import DirectorAgent
from .config import Settings, engine_availability, get_settings
from .llm import LLMClient
from .logging import get_logger
from .schemas import (
    CriticFeedback,
    JobRequest,
    RenderArtifact,
    ScriptPlan,
    ShotSpec,
    StyleGuide,
)

logger = get_logger(__name__)

# 进程启动时间，用于 /healthz 报告 uptime。
_STARTED_AT = time.time()


class PhaseNotImplemented(NotImplementedError):
    """功能尚未实现。

    显式类型而非裸 ``NotImplementedError``：gRPC 适配器据此映射为
    ``UNIMPLEMENTED`` 状态码（而不是 ``INTERNAL``），Go 侧能据此区分
    「服务端还没实现」与「服务端出错了」，前者不该被重试。
    """


class ServiceUnavailable(RuntimeError):
    """依赖不可用（模型、沙盒、存储等）。"""


@dataclass
class HealthStatus:
    """健康与能力报告。"""

    healthy: bool = True
    version: str = __version__
    llm_provider: str = ""
    vlm_model: str = ""
    sandbox_ready: bool = False
    capabilities: list[str] = field(default_factory=list)
    uptime_sec: int = 0
    toolchain: dict[str, bool] = field(default_factory=dict)
    #: 每个渲染引擎的工具链是否就绪（键为引擎名）。
    engines: dict[str, bool] = field(default_factory=dict)


class PipelineService:
    """SciDirector AI 大脑的业务门面。

    线程安全性：实例本身不可变（只持有无状态的 agent 与客户端），
    LLMClient 的用量计数是唯一共享可变状态，其误差不影响正确性。
    Windows/Linux 下 gRPC 线程池会并发调用本类的方法。
    """

    def __init__(self, settings: Settings | None = None, llm: LLMClient | None = None) -> None:
        self.settings = settings or get_settings()
        self.llm = llm or LLMClient(self.settings)
        self._director = DirectorAgent(self.llm)

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------

    def health(self) -> HealthStatus:
        """返回服务与沙盒的就绪状态。

        ``sandbox_ready`` 的判定：**至少有一类渲染引擎的工具链可用**
        （``any(engines.values())``），而不是「所有引擎都可用」。

        为什么是「至少一个」：只有 ffmpeg 的环境依然能产出氛围镜头，
        把这种情况判成 not-ready 会让编排层摘掉一个**确实能干活**的实例。
        缺失的引擎通过 ``capabilities`` 里的 ``engine:<名字>=missing`` 单独暴露，
        运维据此判断「哪些标签暂时不可渲染」。

        需要明确的边界：本函数回答的是**工具链就绪度**，
        **不是**「渲染器是否已实现」。阶段一尚未实现任何渲染器
        （见 docs/ROADMAP.md），因此 ``sandbox_ready=True`` 只代表
        「依赖齐了」，不代表现在就能出片。
        """
        toolchain = self.settings.toolchain_report()
        engines = engine_availability(toolchain)

        capabilities = ["plan"]
        if any(engines.values()):
            capabilities.append("render")
        if toolchain.get("ffmpeg"):
            capabilities.append("compose")
        # 逐引擎暴露可用性：编排层据此提前知道哪些标签当前不可渲染，
        # 而不是等任务跑到那一步才失败。
        for name, ok in sorted(engines.items()):
            capabilities.append(f"engine:{name}={'ok' if ok else 'missing'}")
        capabilities.append("mock-llm" if self.llm.is_mock else "vlm")

        return HealthStatus(
            healthy=True,
            version=__version__,
            llm_provider="mock" if self.llm.is_mock else self.settings.llm_provider,
            vlm_model=self.settings.vlm_model,
            sandbox_ready=any(engines.values()),
            capabilities=capabilities,
            uptime_sec=int(time.time() - _STARTED_AT),
            toolchain=toolchain,
            engines=engines,
        )

    # ------------------------------------------------------------------
    # 导演：脚本 -> 分镜表（阶段一已实现）
    # ------------------------------------------------------------------

    def plan_script(self, request: JobRequest) -> ScriptPlan:
        """执行导演智能体，返回结构化分镜表。

        这是唯一在阶段一即可完整跑通的智能体能力，因此可以端到端验证：
        Go -> gRPC -> 提示词 -> 模型 -> 结构校验 -> 时长修复 -> 回传。
        """
        logger.info(
            "收到剧本规划请求",
            extra={
                "job_id": request.job_id,
                "target_duration_sec": request.target_duration_sec,
                "locale": request.locale,
            },
        )
        return self._director.plan(
            job_id=request.job_id,
            raw_script=request.raw_script,
            style_guide=request.style_guide,
            target_duration_sec=request.target_duration_sec,
            locale=request.locale,
        )

    # ------------------------------------------------------------------
    # 流水线（阶段二）
    # ------------------------------------------------------------------

    def run_pipeline(self, request: JobRequest) -> Iterator[dict]:
        """运行完整的 LangGraph 流水线，逐条产出事件。

        阶段二将实现：plan -> code -> render -> critique ->（不合格则 revise 回边）
        -> advance -> 下一镜头 ... -> compose。

        返回类型是「事件字典的可迭代对象」，这样 gRPC 的流式响应可以直接
        边产生边下发，而不是先跑完整条流水线再一次性返回（那会让前端在
        数十分钟里完全看不到进度）。
        """
        raise PhaseNotImplemented(
            "阶段二实现：LangGraph 多智能体流水线（plan -> code -> render -> critique -> revise）"
        )

    def generate_shot(
        self,
        *,
        job_id: str,
        shot: ShotSpec,
        attempt: int,
        style_guide: StyleGuide,
        feedback: CriticFeedback | None = None,
        output_dir: str = "",
        draft_only: bool = False,
    ) -> tuple[ShotSpec, RenderArtifact]:
        """编码 + 渲染单个镜头。"""
        raise PhaseNotImplemented(
            "阶段二实现：编码智能体（标签路由生成 Manim/D3/代码动画）与沙盒渲染"
        )

    def critique_shot(
        self,
        *,
        job_id: str,
        shot: ShotSpec,
        artifact: RenderArtifact,
        attempt: int,
        style_guide: StyleGuide,
    ) -> CriticFeedback:
        """调用 VLM 审查渲染产物。"""
        raise PhaseNotImplemented("阶段二实现：审查智能体（VLM 抽帧审查与可执行建议生成）")

    def revise_shot(
        self,
        *,
        job_id: str,
        shot: ShotSpec,
        human_comment: str,
        attempt: int,
        style_guide: StyleGuide,
        output_dir: str = "",
    ) -> tuple[ShotSpec, RenderArtifact, CriticFeedback]:
        """人工意见回灌：重写代码、重新渲染并自动复审。"""
        raise PhaseNotImplemented("阶段二实现：人工反馈回灌与单镜头重做链路")
