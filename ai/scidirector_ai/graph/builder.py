"""LangGraph 图装配与流水线运行器。

为什么用 LangGraph 而不是手写 while 循环：
* 本流程天然是**带环的有向图**（critique → revise → code 的回边是核心特征，
  不是异常路径），图模型能把它表达成结构而不是散落的 `continue`；
* 需要**状态持久化**：渲染可能几分钟，进程重启要能续跑；
* 需要**条件路由 + 中断**，这些原语是现成的，手写则要自己维护一大堆状态机。

关于 recursion_limit：默认值 25 对本流程**远远不够**。
一次 8 镜头的任务、每镜头平均 1.5 次尝试、每次尝试 4 个节点，
轻松超过 100 步。因此这里按"镜头数 × 尝试上限 × 每轮节点数"动态放宽，
避免出现"图跑到一半被框架掐断"这种极难定位的故障。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Iterator

from langgraph.graph import END, START, StateGraph

from ..agents.coder import CoderAgent
from ..agents.critic import CriticAgent
from ..agents.director import DirectorAgent
from ..config import Settings
from ..llm import LLMClient
from ..logging import get_logger
from ..sandbox.runner import SandboxRunner
from ..tts.factory import build_tts_provider
from .checkpoint import CheckpointHandle, build_checkpointer
from .nodes import (
    PipelineDeps,
    PipelineError,
    PipelineNodes,
    route_after_advance,
    route_after_code,
    route_after_critique,
    route_after_render,
    route_after_revise,
)
from .state import (
    NODE_ADVANCE,
    NODE_CODE,
    NODE_CRITIQUE,
    NODE_PLAN,
    NODE_RENDER,
    NODE_REVISE,
    PipelineState,
    initial_state,
)

logger = get_logger(__name__)

#: 每个镜头每轮尝试会经过的节点数（code + render + critique + 可能的 revise）。
#: 取 6 而不是 4，留出余量。
NODES_PER_ATTEMPT = 6

#: 镜头数的保守上界（导演智能体对 90 秒视频通常产出 8~15 个镜头）。
ASSUMED_MAX_SHOTS = 24


def build_graph(deps: PipelineDeps, checkpointer: Any = None) -> Any:
    """装配并编译 LangGraph 图。

    边的拓扑（与 docs/DESIGN.md §5.2 的图一致）：

        START ─▶ plan ─▶ code ─┬─(成功)─▶ render ─┬─(成功)─▶ critique ─┬─(通过)─▶ advance
                               │                 │                    │
                               └─(失败)─▶ revise ◀┴─(失败)─────────────┘
                                            │                    └─(熔断)─▶ advance
                                            ├─(额度未尽)─▶ code
                                            └─(熔断)─────▶ advance
        advance ─┬─(还有镜头)─▶ code
                 └─(完成)─────▶ END
    """
    nodes = PipelineNodes(deps)

    graph = StateGraph(PipelineState)
    graph.add_node(NODE_PLAN, nodes.plan)
    graph.add_node(NODE_CODE, nodes.code)
    graph.add_node(NODE_RENDER, nodes.render)
    graph.add_node(NODE_CRITIQUE, nodes.critique)
    graph.add_node(NODE_REVISE, nodes.revise)
    graph.add_node(NODE_ADVANCE, nodes.advance)

    graph.add_edge(START, NODE_PLAN)
    graph.add_edge(NODE_PLAN, NODE_CODE)

    # 显式的路径映射（而不是只给函数）：让"这个 hint 会去哪"在图定义里一眼可见，
    # 也避免函数返回了未声明的节点名时框架给出晦涩的错误。
    graph.add_conditional_edges(
        NODE_CODE, route_after_code, {NODE_RENDER: NODE_RENDER, NODE_REVISE: NODE_REVISE}
    )
    graph.add_conditional_edges(
        NODE_RENDER,
        route_after_render,
        {NODE_CRITIQUE: NODE_CRITIQUE, NODE_REVISE: NODE_REVISE, NODE_ADVANCE: NODE_ADVANCE},
    )
    graph.add_conditional_edges(
        NODE_CRITIQUE,
        route_after_critique,
        {NODE_ADVANCE: NODE_ADVANCE, NODE_REVISE: NODE_REVISE},
    )
    graph.add_conditional_edges(
        NODE_REVISE, route_after_revise, {NODE_CODE: NODE_CODE, NODE_ADVANCE: NODE_ADVANCE}
    )
    graph.add_conditional_edges(
        NODE_ADVANCE, route_after_advance, {NODE_CODE: NODE_CODE, "__end__": END}
    )

    return graph.compile(checkpointer=checkpointer)


@dataclass
class RunOutcome:
    """一次流水线运行的统计结果。"""

    job_id: str
    total_shots: int = 0
    approved: int = 0
    failed: int = 0
    awaiting_human: int = 0
    total_tokens: int = 0

    @property
    def progress(self) -> float:
        if self.total_shots == 0:
            return 0.0
        return round(self.approved / self.total_shots, 4)


class PipelineRunner:
    """把一次生成请求变成一串事件。

    职责：构造依赖 -> 装配图 -> 流式执行 -> 产出事件。
    它**不**关心事件如何被传输（gRPC 流 / HTTP NDJSON），那是适配层的事。
    """

    def __init__(
        self,
        settings: Settings,
        llm: LLMClient | None = None,
        deps: PipelineDeps | None = None,
    ) -> None:
        self.settings = settings
        self.llm = llm or LLMClient(settings)
        self.deps = deps or PipelineDeps(
            settings=settings,
            llm=self.llm,
            director=DirectorAgent(self.llm),
            coder=CoderAgent(self.llm, settings),
            critic=CriticAgent(self.llm, settings),
            # 注意：SandboxRunner 本身无状态（资源上限由每次调用的 ResourceLimits
            # 决定），唯一例外是网络隔离策略 —— 它是部署级不变式，因此从 settings 传入。
            runner=SandboxRunner(
                settings.sandbox_network_isolation,
                settings.sandbox_read_only,
                settings.sandbox_seccomp,
            ),
            # TTS：缺省关闭（SCID_TTS_PROVIDER 为空），此时为 None，
            # 渲染节点不会合成任何音频、也不会产生额外文件 ——
            # 与既有行为逐字节一致。
            tts=build_tts_provider(settings),
        )
        self._checkpoint: CheckpointHandle | None = None
        self._app: Any = None

    # ------------------------------------------------------------------
    # 图的生命周期
    # ------------------------------------------------------------------

    @property
    def checkpointer(self) -> CheckpointHandle:
        """惰性构造 checkpointer（避免进程启动时就要求 Postgres 可用）。"""
        if self._checkpoint is None:
            self._checkpoint = build_checkpointer(self.settings)
        return self._checkpoint

    @property
    def app(self) -> Any:
        """惰性编译图（编译有开销，且首次编译会做 schema 校验）。"""
        if self._app is None:
            self._app = build_graph(self.deps, self.checkpointer.saver)
        return self._app

    def close(self) -> None:
        if self._checkpoint is not None:
            self._checkpoint.close()
            self._checkpoint = None
            self._app = None

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    def run(self, request: Any) -> Iterator[dict[str, Any]]:
        """执行流水线并**逐条产出事件**。

        产出时机是"节点执行完毕立刻产出"，而不是"全跑完再返回" ——
        后者会让前端在数十分钟里完全看不到进度，用户会以为系统挂了。
        """
        job_id = request.job_id
        try:
            state = initial_state(
                job_id=job_id,
                raw_script=request.raw_script,
                style_guide=request.style_guide,
                target_duration_sec=request.target_duration_sec,
                max_attempts_per_shot=request.max_attempts_per_shot,
                locale=request.locale,
            )
        except Exception as exc:  # noqa: BLE001 - 输入构造失败属于致命错误
            yield _fatal_event(job_id, f"构造流水线初始状态失败：{exc}")
            return

        config = {
            "configurable": {"thread_id": request.checkpoint_thread_id or job_id},
            "recursion_limit": self._recursion_limit(request),
        }

        logger.info(
            "流水线开始",
            extra={
                "job_id": job_id,
                "target_duration_sec": request.target_duration_sec,
                "max_attempts": request.max_attempts_per_shot,
                "checkpoint_backend": self.checkpointer.backend,
                "durable": self.checkpointer.durable,
            },
        )

        emitted = 0
        try:
            for chunk in self.app.stream(state, config=config, stream_mode="updates"):
                for _node_name, update in (chunk or {}).items():
                    if not isinstance(update, dict):
                        continue
                    for event in update.get("events") or []:
                        emitted += 1
                        yield event
        except PipelineError as exc:
            # 致命错误（例如导演拆解失败）：明确地把失败事件送出去，
            # 而不是让 gRPC 流直接断开 —— 断开时 Go 侧只能看到 UNAVAILABLE，
            # 会误判成"基础设施故障"并触发无意义的重试。
            logger.error("流水线致命错误", extra={"job_id": job_id, "error": str(exc)})
            yield _fatal_event(job_id, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - 需要兜住框架与三方异常
            logger.exception("流水线异常终止")
            yield _fatal_event(job_id, f"流水线内部错误：{exc}")
            return

        logger.info("流水线结束", extra={"job_id": job_id, "events_emitted": emitted})
        yield _final_event(job_id, self.llm)

    def _recursion_limit(self, request: Any) -> int:
        """按镜头数与尝试上限估算递归上限，并留足余量。"""
        attempts = max(int(getattr(request, "max_attempts_per_shot", 3) or 3), 1)
        estimated = ASSUMED_MAX_SHOTS * attempts * NODES_PER_ATTEMPT
        # 至少 100：即使只有一个镜头，重试路径也可能走很多步。
        return max(100, estimated + 50)


def _fatal_event(job_id: str, message: str) -> dict[str, Any]:
    """构造一条"任务级失败"事件。"""
    return {
        "job_id": job_id,
        "shot_id": "",
        "node": "pipeline",
        "status": "FAILED",
        "message": message,
        "attempt": 0,
        "shot_index": 0,
        "total_shots": 0,
        "progress": 0.0,
        "error": message,
        "artifact": None,
        "feedback": None,
        "ts_unix_ms": int(time.time() * 1000),
        "payload_json": "",
    }


def _final_event(job_id: str, llm: LLMClient) -> dict[str, Any]:
    """构造收尾事件，附带本次运行的成本摘要。

    成本可观测是刻意的：token 是这套系统最直接的可变成本，
    不把它放进事件流，就没法在后端做成本核算（阶段五的输入）。

    **只报「只有 Python 知道」的那部分**：LLM 的 token 与调用次数。
    渲染时长与配音字符数都能从任务状态里推导（产物自带 `render_cost_sec`、
    有配音的镜头就是合成过的那几个），由 Go 侧自己算 ——
    两边各报一份迟早会对不上，而「两处口径不一致」在成本这种数字上格外难查。
    """
    return {
        "job_id": job_id,
        "shot_id": "",
        "node": "pipeline",
        "status": "",
        "message": "流水线结束",
        "attempt": 0,
        "shot_index": 0,
        "total_shots": 0,
        "progress": 0.0,
        "error": "",
        "artifact": None,
        "feedback": None,
        "ts_unix_ms": int(time.time() * 1000),
        "payload_json": json.dumps(
            {
                # summary 保留：已有的事件消费方可能在看它，改结构要有理由。
                "summary": {"total_tokens": llm.usage.total_tokens, "calls": llm.usage.calls},
                # cost 是阶段五成本核算的输入。键名用 snake_case、值全是数字 ——
                # 与 §9 对 payload_json 的约定一致，Go 侧直接反序列化不会变成零值。
                "cost": {
                    "llm_prompt_tokens": llm.usage.prompt_tokens,
                    "llm_completion_tokens": llm.usage.completion_tokens,
                    "llm_total_tokens": llm.usage.total_tokens,
                    "llm_calls": llm.usage.calls,
                },
            },
            ensure_ascii=False,
        ),
    }
