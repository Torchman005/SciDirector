"""checkpoint 快照读取（阶段五·状态对账）。

这一层的价值全在**边界**上：
- 读不到时必须如实返回 found=False 并带原因，而不是抛异常（对账是诊断动作，
  它自己失败不该把调用方拖下去）；
- 状态里的分镜可能是 pydantic 模型、dataclass 或普通 dict（取决于经过了几次
  LangGraph 的序列化往返），三种都要支持 —— 只支持一种的话，某次升级后
  这里会静默取到空串，表现为「checkpoint 里一个镜头都没有」，
  看起来像 checkpoint 被清空了。
"""

from __future__ import annotations

from typing import Any

from scidirector_ai.graph.reconcile import read_snapshot


class _State:
    def __init__(self, values: Any) -> None:
        self.values = values


class _App:
    def __init__(self, state: Any = None, exc: Exception | None = None) -> None:
        self._state = state
        self._exc = exc

    def get_state(self, _config: Any) -> Any:
        if self._exc is not None:
            raise self._exc
        return self._state


class _Handle:
    backend = "postgres"
    detail = "状态持久化到 Postgres"


class _Runner:
    def __init__(self, state: Any = None, exc: Exception | None = None) -> None:
        self.checkpointer = _Handle()
        self.app = _App(state, exc)


def test_reads_facts_without_deriving_status() -> None:
    """只报**事实**（attempt / 有无产物），不推导镜头状态。

    「镜头算 APPROVED 还是 AWAITING_HUMAN」是状态机的判断，属于 Go 侧的领域逻辑。
    这里再推一份就等于有两套状态机，而两套实现迟早会不一致 ——
    那时"对账"本身反而成了新的不一致来源。
    """
    state = _State(
        {
            "finished": True,
            "cursor": 2,
            "shots": [{"shot_id": "s0"}, {"shot_id": "s1"}],
            "attempts": {"s0": 2},
            "artifacts": {"s0": object()},
        }
    )
    snap = read_snapshot(_Runner(state), "job-1")

    assert snap.found is True
    assert snap.finished is True
    assert snap.cursor == 2
    assert snap.backend == "postgres"
    assert snap.shots == {
        "s0": {"attempt": 2, "has_artifact": True},
        "s1": {"attempt": 0, "has_artifact": False},
    }
    # 快照里不该出现任何"状态"字段 —— 那属于 Go 侧。
    for info in snap.shots.values():
        assert set(info) == {"attempt", "has_artifact"}


def test_supports_shot_objects_not_only_dicts() -> None:
    """分镜可能是对象而不是 dict（LangGraph 序列化往返后会变形态）。"""

    class _Spec:
        def __init__(self, shot_id: str) -> None:
            self.shot_id = shot_id

    state = _State({"shots": [_Spec("s0")], "attempts": {}, "artifacts": {}})
    snap = read_snapshot(_Runner(state), "job-1")
    assert "s0" in snap.shots


def test_missing_thread_reports_not_found_without_raising() -> None:
    """线程不存在：found=False，且不抛异常。"""
    snap = read_snapshot(_Runner(_State(None)), "job-1")
    assert snap.found is False
    assert snap.backend == "postgres"
    assert snap.detail  # 必须带原因，报告才能自我解释


def test_read_failure_is_reported_not_raised() -> None:
    """读取抛异常时也不向上抛 —— 诊断动作不该把调用方拖下去。"""
    snap = read_snapshot(_Runner(exc=RuntimeError("连接被拒绝")), "job-1")
    assert snap.found is False
    assert "连接被拒绝" in snap.detail


def test_thread_id_defaults_to_job_id() -> None:
    """thread_id 与 job_id 本来就对齐；不传时应当用 job_id。"""
    seen: list[Any] = []

    class _R(_Runner):
        def __init__(self) -> None:
            super().__init__(_State({"shots": [], "attempts": {}, "artifacts": {}}))
            self.app = _App(_State({"shots": [], "attempts": {}, "artifacts": {}}))

    r = _R()
    r.app.get_state = lambda cfg: (seen.append(cfg), _State({"shots": []}))[1]  # type: ignore[method-assign]
    read_snapshot(r, "job-xyz")
    assert seen[0]["configurable"]["thread_id"] == "job-xyz"
