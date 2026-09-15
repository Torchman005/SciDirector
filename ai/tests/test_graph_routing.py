"""流水线图的路由与端到端行为测试。

两组：

1. **路由表**（纯函数）：每个 route_hint 必须映射到预期节点；
   未登记的 hint 必须**吵闹地失败**而不是走默认分支 ——
   悄悄走默认分支的表现是"流水线莫名结束"，几乎无法排查。
2. **端到端**：用桩依赖跑完整张图，覆盖通过、重做、熔断三条路径；
   再用真实 ffmpeg + mock LLM 跑一次真实流水线，验证"能出片"。

端到端那段是这一层最有价值的测试：它同时验证了
图拓扑、状态合并、事件产出、重试计数与熔断，单测任何一个节点都测不到这些。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from scidirector_ai.agents.coder import CodeArtifact, CodeGenerationResult
from scidirector_ai.agents.critic import CritiqueOutcome
from scidirector_ai.config import Settings
from scidirector_ai.graph.builder import PipelineRunner, build_graph
from scidirector_ai.graph.nodes import (
    HINT_CRITIQUE,
    HINT_DONE,
    HINT_HUMAN,
    HINT_NEXT,
    HINT_OK,
    HINT_RENDER,
    HINT_RETRY,
    PipelineDeps,
    PipelineError,
    PipelineNodes,
    route_after_advance,
    route_after_code,
    route_after_critique,
    route_after_render,
    route_after_revise,
)
from scidirector_ai.graph.state import (
    NODE_ADVANCE,
    NODE_CODE,
    NODE_CRITIQUE,
    NODE_RENDER,
    NODE_REVISE,
    initial_state,
)
from scidirector_ai.llm import LLMClient
from scidirector_ai.schemas import (
    CriticFeedback,
    FeedbackSource,
    JobRequest,
    RenderArtifact,
    SceneTag,
    ShotSpec,
    StyleGuide,
)

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        env="test",
        llm_provider="mock",
        # 显式置空：避免测试去尝试连 Postgres（默认 DSN 非空）。
        postgres_dsn="",
        sandbox_work_dir=str(tmp_path / "sandbox"),
        render_width=320,
        render_height=240,
        render_fps=10,
        manim_timeout_sec=30,
        critic_frame_samples=3,
        **overrides,  # type: ignore[arg-type]
    )


# ===========================================================================
# 路由表
# ===========================================================================


class TestRouteTable:
    def test_code_routes(self) -> None:
        assert route_after_code({**initial_state(
            job_id="j", raw_script="s", style_guide=StyleGuide(),
            target_duration_sec=30, max_attempts_per_shot=3, locale="zh-CN",
        ), "route_hint": HINT_RENDER}) == NODE_RENDER
        assert route_after_code({"route_hint": HINT_RETRY}) == NODE_REVISE  # type: ignore[arg-type]

    def test_render_routes(self) -> None:
        assert route_after_render({"route_hint": HINT_CRITIQUE}) == NODE_CRITIQUE  # type: ignore[arg-type]
        assert route_after_render({"route_hint": HINT_RETRY}) == NODE_REVISE  # type: ignore[arg-type]
        # 不可重试的渲染失败（缺引擎）直接转人工 -> 跳过 critique 直奔 advance
        assert route_after_render({"route_hint": HINT_HUMAN}) == NODE_ADVANCE  # type: ignore[arg-type]

    def test_critique_routes(self) -> None:
        assert route_after_critique({"route_hint": HINT_OK}) == NODE_ADVANCE  # type: ignore[arg-type]
        assert route_after_critique({"route_hint": HINT_RETRY}) == NODE_REVISE  # type: ignore[arg-type]
        assert route_after_critique({"route_hint": HINT_HUMAN}) == NODE_ADVANCE  # type: ignore[arg-type]

    def test_revise_routes(self) -> None:
        assert route_after_revise({"route_hint": HINT_RETRY}) == NODE_CODE  # type: ignore[arg-type]
        assert route_after_revise({"route_hint": HINT_HUMAN}) == NODE_ADVANCE  # type: ignore[arg-type]

    def test_advance_routes(self) -> None:
        assert route_after_advance({"route_hint": HINT_NEXT}) == NODE_CODE  # type: ignore[arg-type]
        assert route_after_advance({"route_hint": HINT_DONE}) == "__end__"  # type: ignore[arg-type]

    def test_unknown_hint_raises(self) -> None:
        """未登记的 hint 是**编程错误**，必须吵闹地失败。

        悄悄走默认分支的表现是"流水线莫名结束"，排查成本极高。
        """
        with pytest.raises(PipelineError) as exc_info:
            route_after_code({"route_hint": "typo"} )  # type: ignore[arg-type]
        assert "typo" in str(exc_info.value)

    def test_missing_hint_uses_default(self) -> None:
        """空 hint 是初始状态（尚未有节点跑过），此时走默认分支是合理的。"""
        assert route_after_code({}) == NODE_RENDER  # type: ignore[arg-type]
        assert route_after_advance({}) == "__end__"  # type: ignore[arg-type]


# ===========================================================================
# 端到端（桩依赖）
# ===========================================================================


class _StubDirector:
    def __init__(self, shots: list[ShotSpec]) -> None:
        self.shots = shots
        self.calls = 0

    def plan(self, **kwargs: Any) -> Any:
        self.calls += 1
        from scidirector_ai.schemas import ScriptPlan

        return ScriptPlan(outline="桩大纲", shots=self.shots, total_tokens=0)


class _StubCoder:
    def __init__(self, policy_ok: bool = True) -> None:
        self.policy_ok = policy_ok
        self.calls: list[int] = []

    def generate(self, *, shot: ShotSpec, attempt: int = 1, **kwargs: Any) -> CodeGenerationResult:
        self.calls.append(attempt)
        return CodeGenerationResult(
            artifact=CodeArtifact(code="class SciShotScene(Scene): pass", language="python"),
            policy_ok=self.policy_ok,
            policy_summary="" if self.policy_ok else "桩：静态检查失败",
        )


class _StubCritic:
    def __init__(self, verdicts: list[bool]) -> None:
        self.verdicts = list(verdicts)
        self.calls = 0

    def review(self, *, shot: ShotSpec, attempt: int, **kwargs: Any) -> CritiqueOutcome:
        self.calls += 1
        passed = self.verdicts.pop(0) if self.verdicts else True
        feedback = CriticFeedback(
            passed=passed,
            score=0.9 if passed else 0.4,
            issues=[] if passed else ["字号过小"],
            suggestions=[] if passed else ["把字号从 24 提到 48"],
            source=FeedbackSource.VLM,
            attempt=attempt,
        )
        return CritiqueOutcome(feedback=feedback, model_passed=passed, program_passed=passed)


class _StubRenderer:
    """产出占位文件的渲染器（不跑真实 ffmpeg）。"""

    engine = "stub"

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def available(self) -> tuple[bool, str]:
        return True, ""

    def render(self, request: Any, runner: Any) -> Any:
        from scidirector_ai.media import MediaInfo
        from scidirector_ai.renderer import RenderResult

        self.calls += 1
        if self.error is not None:
            raise self.error
        out = Path(request.output_dir) / "stub.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"stub")
        return RenderResult(
            video_path=str(out),
            engine="stub",
            media=MediaInfo(path=str(out), duration_sec=request.duration_sec,
                            width=320, height=240, fps=10),
            render_cost_sec=0.1,
        )


def make_deps(
    tmp_path: Path,
    *,
    shots: list[ShotSpec],
    verdicts: list[bool] | None = None,
    policy_ok: bool = True,
    renderer: _StubRenderer | None = None,
) -> tuple[PipelineDeps, _StubDirector, _StubCoder, _StubCritic, _StubRenderer]:
    settings = make_settings(tmp_path)
    llm = LLMClient(settings)
    director = _StubDirector(shots)
    coder = _StubCoder(policy_ok=policy_ok)
    critic = _StubCritic(verdicts or [])
    render = renderer or _StubRenderer()
    deps = PipelineDeps(
        settings=settings, llm=llm,
        director=director,  # type: ignore[arg-type]
        coder=coder,  # type: ignore[arg-type]
        critic=critic,  # type: ignore[arg-type]
        runner=SandboxRunnerStub(),  # type: ignore[arg-type]
    )
    # 所有引擎都用同一个桩渲染器，避免依赖真实引擎是否安装。
    for engine in ("manim", "d3", "echarts", "code_anim", "stock"):
        deps._renderers[engine] = render  # type: ignore[assignment]
    return deps, director, coder, critic, render


class SandboxRunnerStub:
    """沙盒桩：图测试不真的起进程。"""

    def run(self, argv: list[str], **kwargs: Any) -> Any:
        from scidirector_ai.sandbox.runner import ExecResult

        return ExecResult(command=list(argv), returncode=0)


def make_shots(count: int = 2) -> list[ShotSpec]:
    return [
        ShotSpec(shot_id=f"job-x-s{i:03d}", index=i, narration=f"第 {i} 段",
                 visual_brief="画面", tag=SceneTag.AMBIENCE, duration_sec=4.0)
        for i in range(count)
    ]


def run_graph(deps: PipelineDeps, *, shots: int = 2, max_attempts: int = 3,
              monkeypatch: pytest.MonkeyPatch | None = None) -> list[dict[str, Any]]:
    """跑完整张图，返回所有事件。"""
    if monkeypatch is not None:
        # 抽帧依赖真实 ffmpeg；图测试不验证它，替换成空实现。
        import scidirector_ai.graph.nodes as nodes_module

        monkeypatch.setattr(nodes_module, "extract_frames", lambda *a, **k: [])

    state = initial_state(
        job_id="job-x", raw_script="脚本内容足够长", style_guide=StyleGuide(),
        target_duration_sec=30, max_attempts_per_shot=max_attempts, locale="zh-CN",
    )
    app = build_graph(deps)
    events: list[dict[str, Any]] = []
    for chunk in app.stream(state, config={"recursion_limit": 200}, stream_mode="updates"):
        for _node, update in (chunk or {}).items():
            if isinstance(update, dict):
                events.extend(update.get("events") or [])
    return events


class TestEndToEndWithStubs:
    def test_all_shots_pass(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        deps, director, coder, critic, _ = make_deps(tmp_path, shots=make_shots(2))
        events = run_graph(deps, monkeypatch=monkeypatch)

        assert director.calls == 1
        assert coder.calls == [1, 1], "每个镜头应当只尝试一次"
        assert critic.calls == 2
        nodes = [e["node"] for e in events]
        assert nodes.count("plan") == 1
        assert nodes.count("critique") == 2
        assert all(e.get("status") != "FAILED" for e in events)

    def test_rejected_shot_is_retried_then_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """一次不通过 -> 重做 -> 通过。这是闭环的核心路径。"""
        deps, _, coder, critic, _ = make_deps(
            tmp_path, shots=make_shots(1), verdicts=[False, True]
        )
        events = run_graph(deps, max_attempts=3, monkeypatch=monkeypatch)

        assert coder.calls == [1, 2], f"应当重做一次，实际尝试 {coder.calls}"
        assert critic.calls == 2
        assert any(e["node"] == "revise" and e["status"] == "RETRYING" for e in events)
        assert any(e["status"] == "APPROVED" for e in events)

    def test_circuit_breaker_stops_retrying(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**成本控制的核心**：连续不合格时必须在尝试上限处熔断，而不是无限重试。"""
        deps, _, coder, critic, _ = make_deps(
            tmp_path, shots=make_shots(1), verdicts=[False, False, False, False, False]
        )
        events = run_graph(deps, max_attempts=3, monkeypatch=monkeypatch)

        assert coder.calls == [1, 2, 3], f"应当恰好尝试 3 次，实际 {coder.calls}"
        assert critic.calls == 3
        assert any(e["status"] == "AWAITING_HUMAN" for e in events)
        assert any("转人工" in (e.get("message") or "") for e in events)

    def test_circuit_breaker_does_not_block_other_shots(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """一个镜头熔断不该拖死整片 —— 其余镜头可能都是好的。

        这正是"每镜头独立 attempt 计数器"的价值所在。
        """
        deps, _, coder, critic, _ = make_deps(
            tmp_path, shots=make_shots(2), verdicts=[False, False, False, True]
        )
        events = run_graph(deps, max_attempts=3, monkeypatch=monkeypatch)

        # 全量扫描而不是只看 critique：熔断状态由 revise 节点发出。
        statuses = [e["status"] for e in events]
        assert "AWAITING_HUMAN" in statuses, "第一个镜头应当熔断"
        assert "APPROVED" in statuses, "第二个镜头仍应正常通过"
        # 两个镜头都应当被处理到（第一个熔断后没有中断整条流水线）。
        assert len([e for e in events if e["node"] == "critique"]) == 4

    def test_static_failure_skips_render(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """静态检查失败必须**跳过渲染** —— 那正是省钱的地方。"""
        deps, _, coder, _, renderer = make_deps(
            tmp_path, shots=make_shots(1), policy_ok=False
        )
        run_graph(deps, max_attempts=2, monkeypatch=monkeypatch)

        assert renderer.calls == 0, "静态检查失败却仍然渲染了"
        assert coder.calls == [1, 2], "应当把违规回灌重写"

    def test_non_retryable_render_failure_goes_to_human(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """缺引擎这类环境问题：不重试，直接转人工。"""
        from scidirector_ai.renderer import RendererError

        broken = _StubRenderer(error=RendererError("缺引擎", retryable=False))
        deps, _, coder, critic, _ = make_deps(
            tmp_path, shots=make_shots(1), renderer=broken
        )
        events = run_graph(deps, max_attempts=3, monkeypatch=monkeypatch)

        assert coder.calls == [1], "不可重试的失败不该再重写代码"
        assert critic.calls == 0, "渲染失败不该进入审查"
        assert any(e["status"] == "AWAITING_HUMAN" for e in events)

    def test_events_carry_progress_and_shot_ids(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """事件必须能被前端直接消费：要有 shot_id、序号与进度。"""
        deps, _, _, _, _ = make_deps(tmp_path, shots=make_shots(2))
        events = run_graph(deps, monkeypatch=monkeypatch)

        shot_events = [e for e in events if e.get("shot_id")]
        assert shot_events, "没有任何带 shot_id 的事件"
        assert all(e["shot_index"] in (0, 1) for e in shot_events)
        assert all(0.0 <= e["progress"] <= 1.0 for e in events)
        assert all(e["ts_unix_ms"] > 0 for e in events)


# ===========================================================================
# 真实流水线（mock LLM + 真实 ffmpeg）
# ===========================================================================


@pytest.mark.skipif(not HAS_FFMPEG, reason="需要 ffmpeg")
class TestRealPipeline:
    """真实跑一次：mock LLM 出题 + 真实 ffmpeg 渲染 + 真实熔断。

    这是**唯一**能同时验证"图能跑通"与"确实产出了视频文件"的测试。
    mock 剧本产出 4 个镜头（氛围/数学/数据/氛围）：
    本机没有 Manim 与 Playwright，因此数学与数据镜头会因"缺引擎"
    （不可重试）直接转人工，而两个氛围镜头会真实出片并通过审查。
    """

    def test_pipeline_produces_real_videos(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        runner = PipelineRunner(settings)
        request = JobRequest(
            job_id="job-real", raw_script="从勾股定理出发，用面积法证明。" * 3,
            target_duration_sec=30, max_attempts_per_shot=2,
        )

        events = list(runner.run(request))
        assert events, "流水线没有产出任何事件"

        nodes = [e["node"] for e in events]
        assert "plan" in nodes and "render" in nodes and "critique" in nodes

        # 氛围镜头（stock）应当真实出片。
        artifacts = [e["artifact"] for e in events if e.get("artifact")]
        assert artifacts, "没有产出任何渲染产物"
        for artifact in artifacts:
            path = Path(artifact.video_path)
            assert path.is_file() and path.stat().st_size > 0
            assert artifact.duration_sec > 0
            assert artifact.frame_samples, "没有抽帧，审查会降级"

        # 至少有一个镜头通过审查（即氛围镜头）。
        assert any(e["status"] == "APPROVED" for e in events)

        # 缺引擎的镜头应当转人工而不是让任务崩掉。
        assert any(e["status"] == "AWAITING_HUMAN" for e in events)

        # 最后一个事件必须存在，前端才知道流结束了。
        assert events[-1]["node"] == "pipeline"
        runner.close()

    def test_pipeline_emits_token_summary(self, tmp_path: Path) -> None:
        """收尾事件必须带 token 成本摘要 —— 阶段五的成本核算依赖它。"""
        import json as _json

        settings = make_settings(tmp_path)
        runner = PipelineRunner(settings)
        request = JobRequest(
            job_id="job-cost", raw_script="短脚本内容占位。" * 3,
            target_duration_sec=20, max_attempts_per_shot=1,
        )
        events = list(runner.run(request))
        final = events[-1]
        payload = _json.loads(final["payload_json"])
        assert "total_tokens" in payload["summary"]
        runner.close()
