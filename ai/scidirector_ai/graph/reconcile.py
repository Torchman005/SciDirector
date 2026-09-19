"""状态对账：读取 LangGraph checkpoint 里的续跑状态。

## 为什么这件事必须由 Python 做

checkpoint 的**存储格式是 LangGraph 的实现细节**：表结构、序列化方式、
状态字段的排布都可能随版本变化。让 Go 去直接读 Postgres，等于把两个项目的内部
实现焊在一起 —— 一次 LangGraph 升级就会静默读出错的数据，而且不会有任何报错。
因此由持有方（Python）读取，对外只暴露**语义化字段**
（见 `proto/scidirector/v1/ai_service.proto` 的 `CheckpointSnapshotResponse`）。

## 这里**不**推导镜头的「状态」

checkpoint 里有的是续跑所需的事实：处理到第几个镜头、每个镜头试了几次、
有没有产物。而「镜头是 APPROVED 还是 AWAITING_HUMAN」是**状态机的判断**，
它属于 Go 侧的领域逻辑（`domain.Transition`）。

如果这里也推导一份，就会有两套状态机 —— 而两套实现迟早会不一致，
届时"对账"本身反而成了新的不一致来源。因此快照只报**事实**，
判断留给对账逻辑（`domain` 里的纯函数 + Go 侧的比较）。
"""

from __future__ import annotations

from typing import Any

from ..logging import get_logger

logger = get_logger(__name__)


class CheckpointSnapshot:
    """一次 checkpoint 读取的结果（与 proto 的字段一一对应）。"""

    def __init__(
        self,
        *,
        found: bool,
        finished: bool = False,
        cursor: int = 0,
        shots: dict[str, dict[str, Any]] | None = None,
        backend: str = "",
        detail: str = "",
    ) -> None:
        self.found = found
        self.finished = finished
        self.cursor = cursor
        self.shots = shots or {}
        self.backend = backend
        self.detail = detail


def read_snapshot(runner: Any, job_id: str, thread_id: str = "") -> CheckpointSnapshot:
    """读取指定线程的 checkpoint 快照。

    **绝不抛异常**：对账是一个诊断动作，它自己失败不该把调用方（Go 的 worker）
    也拖下去。读不到就如实返回 `found=False` 并带上原因 —— 而这恰恰是对账需要
    知道的信息之一（"checkpoint 里没有这个线程"本身就是一种值得报告的情况）。

    `runner` 是 `PipelineRunner`；传入而不是内部构造，便于测试注入。
    """
    tid = thread_id or job_id
    handle = runner.checkpointer
    backend = getattr(handle, "backend", "")
    detail = getattr(handle, "detail", "")

    try:
        state = runner.app.get_state({"configurable": {"thread_id": tid}})
    except Exception as exc:  # noqa: BLE001 - 诊断动作绝不向上抛
        logger.warning(
            "读取 checkpoint 失败", extra={"job_id": job_id, "thread_id": tid, "error": str(exc)}
        )
        return CheckpointSnapshot(
            found=False, backend=backend, detail=f"读取失败：{exc}"
        )

    # LangGraph 的 StateSnapshot：没有检查点时 values 为空。
    values = getattr(state, "values", None)
    if not values:
        # 「线程不存在」与「存在但状态为空」在这里无法区分，也不重要 ——
        # 对账关心的是"有没有可用的续跑状态"。原因由 backend/detail 说明。
        return CheckpointSnapshot(
            found=False,
            backend=backend,
            detail=detail or "checkpoint 中没有该线程的可用状态",
        )

    raw_shots = values.get("shots") or []
    attempts = values.get("attempts") or {}
    artifacts = values.get("artifacts") or {}

    shots: dict[str, dict[str, Any]] = {}
    for spec in raw_shots:
        shot_id = _shot_id(spec)
        if not shot_id:
            continue
        shots[shot_id] = {
            "attempt": int(attempts.get(shot_id, 0) or 0),
            "has_artifact": shot_id in artifacts,
        }

    return CheckpointSnapshot(
        found=True,
        finished=bool(values.get("finished", False)),
        cursor=int(values.get("cursor", 0) or 0),
        shots=shots,
        backend=backend,
        detail=detail,
    )


def _shot_id(spec: Any) -> str:
    """从状态里的分镜条目取 shot_id。

    状态里的分镜可能是 pydantic 模型、dataclass 或普通 dict
    （取决于它经过了几次 LangGraph 的序列化往返），因此三种都支持 ——
    只支持一种的话，某次升级后这里会静默取到空串，表现为
    「checkpoint 里一个镜头都没有」，看起来像 checkpoint 被清空了。
    """
    if isinstance(spec, dict):
        return str(spec.get("shot_id") or "")
    return str(getattr(spec, "shot_id", "") or "")
