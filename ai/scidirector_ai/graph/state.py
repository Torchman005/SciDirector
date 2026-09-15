"""LangGraph 流水线状态定义。

这是多智能体编排的**数据契约**：所有节点读它、改它，LangGraph 负责在节点间传递。

三个关键设计决策：

1. **状态里只放"可序列化的小对象"**。
   视频二进制永远走文件系统，状态里只存路径。原因是 LangGraph 会把状态
   写进 Postgres checkpoint，塞进大对象会让 checkpoint 体积爆炸、恢复变慢。

2. **``attempt`` 是每镜头独立计数器**。
   进入下一镜头时归零。这是防止"一个坏镜头把整个任务拖死"的关键 ——
   若用全局计数器，第 3 个镜头失败会直接触发熔断，让后面 20 个好镜头无从生成。

3. **用 reducer 累加而不是覆盖**。
   ``events`` 与 ``errors`` 用 ``operator.add``，让并发/多次调用天然合并，
   这是 LangGraph 的惯用法，也避免了节点之间互相覆盖彼此的产物。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from ..schemas import CriticFeedback, RenderArtifact, ShotSpec, StyleGuide

# 流水线节点名常量。
# 用常量而非裸字符串：节点名会出现在事件流、日志与前端展示中，
# 拼错一个字母的后果是「事件静默丢失」，很难排查。
NODE_PLAN = "plan"
NODE_CODE = "code"
NODE_RENDER = "render"
NODE_CRITIQUE = "critique"
NODE_REVISE = "revise"
NODE_ADVANCE = "advance"
NODE_COMPOSE = "compose"

# 结束哨兵：条件边用它表示「没有下一个镜头了」。
END_CURSOR = -1


class PipelineState(TypedDict, total=False):
    """整条生成流水线的共享状态。

    ``total=False`` 让所有字段可选：LangGraph 允许节点只写自己负责的字段，
    强求全量会逼迫每个节点重复写一遍无关字段（容易出错）。
    """

    # ------------------------------------------------------------------
    # 输入（整个生命周期内不变）
    # ------------------------------------------------------------------
    job_id: str
    raw_script: str
    style_guide: StyleGuide
    target_duration_sec: float
    max_attempts_per_shot: int
    locale: str

    # ------------------------------------------------------------------
    # 导演产出
    # ------------------------------------------------------------------
    outline: str
    shots: list[ShotSpec]

    # ------------------------------------------------------------------
    # 当前镜头的工作区
    # ------------------------------------------------------------------
    cursor: int                     # 当前处理到第几个镜头（索引）
    current_code: str               # 当前镜头生成的源码
    current_language: str           # python / html+js
    render_error: str               # 渲染失败的技术信息（编译错误等）

    # ------------------------------------------------------------------
    # 产物与审查（按 shot_id 索引，便于 HITL 单点重做时直接命中）
    # ------------------------------------------------------------------
    artifacts: dict[str, RenderArtifact]
    feedback: dict[str, CriticFeedback]
    attempts: dict[str, int]        # 每镜头已尝试次数（熔断依据）
    human_feedback: dict[str, str]  # 人类打回意见（HITL 注入点）

    # ------------------------------------------------------------------
    # 事件与统计（用 reducer 累加，允许并发节点合并写入）
    # ------------------------------------------------------------------
    events: Annotated[list[dict[str, Any]], operator.add]
    errors: Annotated[list[str], operator.add]

    # ------------------------------------------------------------------
    # 成本与可观测
    # ------------------------------------------------------------------
    total_tokens: int
    started_at_ms: int
    finished: bool


def initial_state(
    *,
    job_id: str,
    raw_script: str,
    style_guide: StyleGuide,
    target_duration_sec: float,
    max_attempts_per_shot: int,
    locale: str,
) -> PipelineState:
    """构造初始状态。

    集中构造（而不是让调用方手写 dict）的好处：新增字段时只有一处需要改，
    且所有容器字段都被正确初始化 —— 忘记初始化 ``attempts`` 会直接导致 KeyError。
    """
    return PipelineState(
        job_id=job_id,
        raw_script=raw_script,
        style_guide=style_guide,
        target_duration_sec=target_duration_sec,
        max_attempts_per_shot=max_attempts_per_shot,
        locale=locale,
        outline="",
        shots=[],
        cursor=0,
        current_code="",
        current_language="",
        render_error="",
        artifacts={},
        feedback={},
        attempts={},
        human_feedback={},
        events=[],
        errors=[],
        total_tokens=0,
        started_at_ms=0,
        finished=False,
    )


def current_shot(state: PipelineState) -> ShotSpec | None:
    """返回当前游标指向的镜头；越界返回 None。

    所有节点都必须通过本函数取当前镜头，禁止直接 ``state["shots"][cursor]`` ——
    越界索引会抛 IndexError 并让整个任务崩掉，而越界在并发/重试场景下是正常情况。
    """
    shots = state.get("shots") or []
    cursor = state.get("cursor", 0)
    if 0 <= cursor < len(shots):
        return shots[cursor]
    return None


def shot_attempt(state: PipelineState, shot_id: str) -> int:
    """读取某镜头已尝试次数（不存在视为 0）。"""
    return int((state.get("attempts") or {}).get(shot_id, 0))


def progress_ratio(state: PipelineState) -> float:
    """整体进度：已通过审查的镜头占比。"""
    shots = state.get("shots") or []
    if not shots:
        return 0.0
    feedback = state.get("feedback") or {}
    passed = sum(1 for s in shots if feedback.get(s.shot_id) and feedback[s.shot_id].passed)
    return round(passed / len(shots), 4)


def make_event(
    state: PipelineState,
    *,
    node: str,
    message: str,
    status: str = "",
    shot: ShotSpec | None = None,
    attempt: int = 0,
    error: str = "",
    artifact: RenderArtifact | None = None,
    feedback: CriticFeedback | None = None,
    payload_json: str = "",
) -> dict[str, Any]:
    """构造一条待推送的流水线事件。

    统一构造保证字段齐全：漏掉 ``ts_unix_ms`` 会让 Go 侧用当前时间兜底，
    在断点续跑场景下产生"时间倒流"的假象，从而干扰排查。

    ``shot`` 为空时自动取当前游标指向的镜头 —— 绝大多数调用点都只关心
    "当前正在处理的镜头"，强制每个调用点自己取一次既啰嗦又容易忘。
    """
    import time

    shots = state.get("shots") or []
    if shot is None:
        shot = current_shot(state)

    return {
        "job_id": state.get("job_id", ""),
        "shot_id": shot.shot_id if shot else "",
        "node": node,
        "status": status,
        "message": message,
        "attempt": attempt,
        # 事件的 shot_index 优先取镜头自身的序号；没有镜头时回落到游标，
        # 这样任务级事件（如 compose）也能正确定位进度位置。
        "shot_index": shot.index if shot else int(state.get("cursor", 0) or 0),
        "total_shots": len(shots),
        "progress": progress_ratio(state),
        "error": error,
        "artifact": artifact,
        "feedback": feedback,
        "ts_unix_ms": int(time.time() * 1000),
        "payload_json": payload_json,
    }
