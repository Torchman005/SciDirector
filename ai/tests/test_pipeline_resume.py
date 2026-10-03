"""断点续跑（resume）的决策测试。

守的是一个具体且代价高昂的缺陷：Go 侧在**重试投递**时会带 ``resume=true``
（见 backend/internal/worker/processor.go），但 Python 侧原先**完全没有读它** ——
每次重试都拿一份全新的 initial_state 喂进同一个 thread，把 shots / cursor /
artifacts / attempts 全部清空，等于从头再跑一遍，**包括已经通过审查的镜头**。
用户看到的现象就是"已通过审查的有时会重新生成"。

这里用一个桩 app 替掉真实图：只需要观察 "喂给图的输入是什么"，
不需要真的跑渲染 —— 而这个决策一旦错了，代价是整条流水线重来一遍。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scidirector_ai.config import Settings
from scidirector_ai.graph.builder import (
    _CONTINUE,
    _FRESH,
    _SKIP_FINISHED,
    PipelineRunner,
)
from scidirector_ai.graph.checkpoint import CheckpointHandle
from scidirector_ai.graph.state import initial_state


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    """与 test_graph_routing.py 保持同一套最小设置。

    显式把 ``postgres_dsn`` 置空：否则测试会去尝试连 Postgres，
    在没跑数据库的机器上白白等待超时。
    """
    return Settings(
        env="test",
        llm_provider="mock",
        postgres_dsn="",
        sandbox_work_dir=str(tmp_path / "sandbox"),
        render_width=320,
        render_height=240,
        render_fps=10,
        manim_timeout_sec=30,
        critic_frame_samples=3,
        **overrides,  # type: ignore[arg-type]
    )


class _StubApp:
    """记录"图被喂了什么"的桩。

    ``graph_input is None`` 意味着"从 checkpoint 继续"；
    是 dict 则意味着"这是一次全新的状态更新"（会把进度清空）。
    """

    def __init__(self, values: dict[str, Any] | None) -> None:
        self._values = values
        self.inputs: list[Any] = []

    def get_state(self, _config: dict[str, Any]) -> Any:
        return SimpleNamespace(values=self._values)

    def stream(self, graph_input: Any, config: dict[str, Any], stream_mode: str) -> Any:
        self.inputs.append(graph_input)
        # 不产出任何 chunk：本测试只关心"喂了什么"，不关心图怎么跑。
        return iter(())


def _request(**over: Any) -> SimpleNamespace:
    base = dict(
        job_id="job-x",
        raw_script="脚本",
        style_guide=None,
        target_duration_sec=30.0,
        max_attempts_per_shot=3,
        locale="zh-CN",
        resume=False,
        checkpoint_thread_id="job-x",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _runner(tmp_path: Any, values: dict[str, Any] | None) -> tuple[PipelineRunner, _StubApp]:
    runner = PipelineRunner(make_settings(tmp_path))
    app = _StubApp(values)
    runner._app = app  # type: ignore[attr-defined]
    runner._checkpoint = CheckpointHandle(  # type: ignore[attr-defined]
        saver=None, backend="stub", durable=False
    )
    return runner, app


def _run(runner: PipelineRunner, request: Any) -> list[dict[str, Any]]:
    return list(runner.run(request))


def _values(*, finished: bool, cursor: int = 3) -> dict[str, Any]:
    """构造一份"看起来像跑过一半"的 checkpoint 状态。"""
    return {
        "shots": [{"shot_id": f"s{i}"} for i in range(5)],
        "cursor": cursor,
        "attempts": {},
        "artifacts": {},
        "finished": finished,
    }


class TestResumeDecision:
    def test_first_run_feeds_full_state(self, tmp_path: Any) -> None:
        """首跑必须喂完整初始状态 —— 那时还没有任何 checkpoint。"""
        runner, app = _runner(tmp_path, _values(finished=False))
        _run(runner, _request(resume=False))
        assert len(app.inputs) == 1
        assert isinstance(app.inputs[0], dict), "首跑应当喂入初始化状态"
        assert app.inputs[0]["cursor"] == 0

    def test_retry_with_unfinished_checkpoint_continues(self, tmp_path: Any) -> None:
        """有未完成的 checkpoint 时，必须喂 None 让图从断点继续。

        喂 dict 会把 shots / cursor 清空 —— 那正是"续跑变成重来"的成因。
        """
        runner, app = _runner(tmp_path, _values(finished=False, cursor=3))
        _run(runner, _request(resume=True))
        assert app.inputs == [None], f"续跑应当喂 None，实际喂了 {app.inputs!r}"

    def test_retry_after_finish_does_not_rerun(self, tmp_path: Any) -> None:
        """上一轮已跑完时，**一次都不该再跑**。

        这是"已通过审查的镜头被重新生成"最直接的那条路径：
        流水线跑完了，但 Go 侧的任务因超时/取消被重投，于是带 resume=true 再来一次。
        """
        runner, app = _runner(tmp_path, _values(finished=True))
        events = _run(runner, _request(resume=True))
        assert app.inputs == [], f"已完成的线程不该再被喂输入，实际 {app.inputs!r}"
        # 仍然要产出收尾事件：Go 侧据此知道这一趟结束了（然后自己重算状态）。
        assert events and events[-1]["node"] == "pipeline"

    def test_retry_without_checkpoint_falls_back_to_fresh(self, tmp_path: Any) -> None:
        """没有 checkpoint 时必须回落到从头跑，而不是报错。

        内存 checkpointer 在 AI 进程重启后就是空的（日志会告警 durable=False），
        此时"没有断点"是完全正常的情况 —— 报错会让这类重试永久失败。
        """
        runner, app = _runner(tmp_path, None)
        _run(runner, _request(resume=True))
        assert isinstance(app.inputs[0], dict), "没有断点时应回落到完整初始状态"

    def test_decision_names_are_stable(self) -> None:
        """三个决策名会被写进日志与排查话术，不要在重构时悄悄改掉。"""
        assert (_FRESH, _CONTINUE, _SKIP_FINISHED) == ("fresh", "continue", "skip_finished")


def test_initial_state_has_empty_progress() -> None:
    """反向对照：正因为初始状态是空的，把它喂给已有进度的线程才会清空进度。

    这条把"为什么必须区分两种输入"钉在代码上 —— 如果哪天 initial_state
    变成"保留已有进度"，上面的推理就需要重新审视。
    """
    state = initial_state(
        job_id="j",
        raw_script="s",
        style_guide=None,  # type: ignore[arg-type]
        target_duration_sec=10.0,
        max_attempts_per_shot=3,
        locale="zh-CN",
    )
    assert state["cursor"] == 0
    assert state["shots"] == []
    assert state["artifacts"] == {}
    assert state["attempts"] == {}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
