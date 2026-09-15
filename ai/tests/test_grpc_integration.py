"""gRPC 端到端集成测试。

一次性验证四个最容易出错的接缝：
    1. protoc 生成物能否被正确导入（pb/__init__.py 的 sys.path shim）；
    2. proto <-> Pydantic 的枚举与字段转换是否正确；
    3. 异常 -> gRPC 状态码的映射是否符合「Go 侧据此决定要不要重试」的约定；
    4. 导演 / 编码 / 渲染 / 审查四条链路能否真正跑通。

测试使用 **mock LLM**，因此不依赖网络与密钥；但**渲染是真的**（ffmpeg），
因为"渲染是否真的产出了可播放的文件"是这一层最值得验证的事。
"""
from __future__ import annotations

import shutil
import socket
import tempfile
from pathlib import Path

import pytest

grpc = pytest.importorskip("grpc", reason="未安装 grpcio，跳过 gRPC 集成测试")

from scidirector_ai.config import Settings  # noqa: E402
from scidirector_ai.grpc_server import build_server  # noqa: E402
from scidirector_ai.service import PipelineService  # noqa: E402

# pb/__init__.py 在导入时完成 sys.path 注入。
from scidirector.v1 import ai_service_pb2 as pb  # noqa: E402
from scidirector.v1 import ai_service_pb2_grpc as pbg  # noqa: E402
from scidirector.v1 import common_pb2 as common  # noqa: E402

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def _free_port() -> int:
    """向操作系统要一个空闲端口。

    硬编码端口在 CI 上极易与并行任务冲突，导致随机失败 —— 那类失败最消耗信任。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    work = tmp_path_factory.mktemp("integration")
    return Settings(
        env="test",
        llm_provider="mock",
        postgres_dsn="",  # 显式置空：避免测试去连 Postgres
        ai_grpc_host="127.0.0.1",
        ai_grpc_port=_free_port(),
        grpc_max_workers=4,
        sandbox_work_dir=str(work),
        render_width=320,
        render_height=240,
        render_fps=10,
        manim_timeout_sec=30,
        critic_frame_samples=3,
    )


@pytest.fixture(scope="module")
def service(settings: Settings):
    svc = PipelineService(settings)
    yield svc
    svc.close()


@pytest.fixture(scope="module")
def grpc_stub(settings: Settings, service: PipelineService):
    """启动一个真实的 gRPC server，返回 stub。

    用 module 作用域：服务器启动/停止与渲染都有固定开销，不必每个用例重来。
    """
    server = build_server(settings, service)
    server.start()

    channel = grpc.insecure_channel(f"127.0.0.1:{settings.ai_grpc_port}")
    grpc.channel_ready_future(channel).result(timeout=10)
    stub = pbg.AiDirectorServiceStub(channel)

    yield stub

    channel.close()
    server.stop(0)


@pytest.fixture()
def output_dir() -> Path:
    with tempfile.TemporaryDirectory(prefix="scid-int-") as tmp:
        yield Path(tmp)


# ===========================================================================
# Health
# ===========================================================================


class TestHealth:
    def test_reports_capabilities(self, grpc_stub) -> None:
        # HealthRequest/HealthResponse 定义在 common.proto（跨服务共用的基础契约），
        # 因此这里必须用 common_pb2 而不是 ai_service_pb2。
        resp = grpc_stub.Health(common.HealthRequest())
        assert resp.healthy is True
        assert resp.version
        assert "plan" in resp.capabilities
        # 阶段二后流水线已可用 —— 冒烟脚本据此自动切换预期。
        assert "pipeline" in resp.capabilities
        assert "code" in resp.capabilities and "critique" in resp.capabilities

    def test_exposes_toolchain_and_engines(self, grpc_stub) -> None:
        """工具链与逐引擎可用性都要透出，编排层才能提前知道哪些标签不可渲染。"""
        resp = grpc_stub.Health(common.HealthRequest())
        assert any(c.startswith("tool:") for c in resp.capabilities)
        engine_caps = [c for c in resp.capabilities if c.startswith("engine:")]
        assert len(engine_caps) >= 5, f"逐引擎能力缺失：{engine_caps}"


# ===========================================================================
# PlanScript
# ===========================================================================


class TestPlanScript:
    def test_returns_valid_storyboard(self, grpc_stub) -> None:
        resp = grpc_stub.PlanScript(
            pb.PlanScriptRequest(
                job_id="job-it-1",
                raw_script="从质能方程出发，推导并验证其结论。" * 6,
                target_duration_sec=60,
            )
        )
        assert len(resp.shots) > 0

        # 业务硬约束：时长之和必须落在 ±10% 以内。
        total = sum(s.duration_sec for s in resp.shots)
        assert 54 <= total <= 66, f"时长预算未被修复：{total}"

        # 索引必须连续且从 0 开始（顺序即播放顺序）。
        assert [s.index for s in resp.shots] == list(range(len(resp.shots)))

        for shot in resp.shots:
            assert shot.shot_id, "shot_id 必须被派生"
            assert shot.engine != common.RENDER_ENGINE_UNSPECIFIED, "引擎必须由标签推导"
            assert shot.tag != common.SCENE_TAG_UNSPECIFIED, "标签必须被归一化"
            assert shot.visual_brief, "视觉意图不能为空（编码智能体依赖它）"
            assert 2.0 <= shot.duration_sec <= 25.0

    def test_outline_is_returned(self, grpc_stub) -> None:
        resp = grpc_stub.PlanScript(
            pb.PlanScriptRequest(
                job_id="job-it-2", raw_script="讲解梯度下降。" * 10, target_duration_sec=45
            )
        )
        assert resp.outline or resp.shots


# ===========================================================================
# RunPipeline（服务端流式）
# ===========================================================================


@pytest.mark.skipif(not HAS_FFMPEG, reason="需要 ffmpeg")
class TestRunPipelineStreaming:
    """一次真实流水线运行，多个角度断言。

    刻意共用**同一次**运行：每次 RunPipeline 都会真实渲染若干氛围镜头
    （每个 1~2 秒），跑四遍会让这个文件从 30 秒涨到 80 秒以上。
    共享结果不影响覆盖度 —— 断言的是同一条事件流的不同侧面。
    """

    @pytest.fixture(scope="class")
    def stream_events(self, grpc_stub) -> list:
        events = list(
            grpc_stub.RunPipeline(
                pb.RunPipelineRequest(
                    job_id="job-it-stream",
                    raw_script="从勾股定理出发，用面积法证明，再用数据说明其应用。" * 3,
                    target_duration_sec=30,
                    max_attempts_per_shot=2,
                )
            )
        )
        assert events, "流没有任何事件"
        return events

    def test_streams_events_to_completion(self, stream_events: list) -> None:
        """流水线必须边跑边推事件，并在结束时**正常关闭流**。

        mock 剧本产出 4 个镜头（氛围/数学/数据/氛围）：
        本机没有 Manim 与 Playwright，数学与数据镜头会因缺引擎转人工，
        两个氛围镜头会真实出片并通过审查。
        """
        nodes = [e.node for e in stream_events]
        assert "plan" in nodes
        assert "code" in nodes
        assert "render" in nodes
        assert "critique" in nodes
        # 收尾事件存在，客户端才知道流正常结束（而不是被掐断）。
        assert stream_events[-1].node == "pipeline"

    def test_plan_event_carries_storyboard_payload(self, stream_events: list) -> None:
        """plan 事件的 payload_json 必须带完整分镜表 —— Go 侧据此落库。

        这是跨语言的关键约定，格式错了两侧就断链（详见 docs/API.md）。
        """
        import json

        plan_events = [e for e in stream_events if e.node == "plan"]
        assert plan_events, "没有 plan 事件"

        payload = json.loads(plan_events[0].payload_json)
        assert "shots" in payload and payload["shots"]
        first = payload["shots"][0]
        # 必须是 snake_case + 数值枚举（Go 用 encoding/json 解到 pb.ShotSpec）。
        assert "shot_id" in first and "visual_brief" in first
        assert isinstance(first["tag"], int)
        assert isinstance(first["engine"], int)

    def test_events_have_progress_and_timestamps(self, stream_events: list) -> None:
        assert all(0.0 <= e.progress <= 1.0 for e in stream_events)
        assert all(e.ts_unix_ms > 0 for e in stream_events)
        # 至少有一条带 shot_id 的事件，前端才能把进度挂到具体镜头。
        assert any(e.shot_id for e in stream_events)

    def test_missing_engines_degrade_to_human_not_crash(self, stream_events: list) -> None:
        """缺引擎的镜头应当转人工，而不是让整条流水线崩掉。

        这条断言守护的是"环境不完整时系统仍能交付部分成果"这一产品决策。
        注意比较的是**枚举值**而不是 ``Name()`` 返回的带前缀字符串。
        """
        statuses = {e.status for e in stream_events}
        assert common.SHOT_STATUS_APPROVED in statuses, "氛围镜头应当通过"
        assert common.SHOT_STATUS_AWAITING_HUMAN in statuses, "缺引擎的镜头应当转人工"


# ===========================================================================
# 单镜头 RPC
# ===========================================================================


@pytest.mark.skipif(not HAS_FFMPEG, reason="需要 ffmpeg")
class TestSingleShotRPCs:
    @staticmethod
    def _ambience_shot() -> common.ShotSpec:
        return common.ShotSpec(
            shot_id="job-it-s000",
            index=0,
            narration="开场：提出问题。",
            visual_brief="标题淡入",
            tag=common.SCENE_TAG_AMBIENCE,
            engine=common.RENDER_ENGINE_STOCK,
            duration_sec=2.0,
        )

    def test_generate_shot_produces_a_video(self, grpc_stub, output_dir: Path) -> None:
        """GenerateShot 必须真的产出一个可解析的视频文件。

        氛围镜头走 ffmpeg 程序化生成，因此这条路径在任何环境（只要有 ffmpeg）
        都能跑通 —— 它也是整条链路最可靠的"最小可验证单元"。
        """
        resp = grpc_stub.GenerateShot(
            pb.GenerateShotRequest(
                job_id="job-it-gen",
                shot=self._ambience_shot(),
                attempt=1,
                output_dir=str(output_dir),
            )
        )
        assert resp.success is True, resp.error
        assert resp.artifact.video_path
        assert Path(resp.artifact.video_path).is_file()
        assert resp.artifact.duration_sec > 0
        assert resp.artifact.width > 0 and resp.artifact.height > 0
        assert resp.artifact.engine == "stock"

    def test_critique_shot_returns_feedback(self, grpc_stub, output_dir: Path) -> None:
        """审查需要抽帧；先生成一次拿到带抽帧的产物。"""
        generated = grpc_stub.GenerateShot(
            pb.GenerateShotRequest(
                job_id="job-it-crit", shot=self._ambience_shot(), attempt=1,
                output_dir=str(output_dir),
            )
        )
        assert generated.artifact.frame_samples, "产物没有抽帧，审查会降级"

        resp = grpc_stub.CritiqueShot(
            pb.CritiqueShotRequest(
                job_id="job-it-crit",
                shot=self._ambience_shot(),
                artifact=generated.artifact,
                attempt=1,
            )
        )
        assert resp.feedback.model
        assert 0.0 <= resp.feedback.score <= 1.0
        # 不通过时必须给出可执行建议 —— 这是回灌重试的前提。
        if not resp.feedback.passed:
            assert resp.feedback.suggestions

    def test_critique_without_frames_degrades(self, grpc_stub) -> None:
        """没有抽帧时必须降级转人工，**绝不能**盲判通过。"""
        resp = grpc_stub.CritiqueShot(
            pb.CritiqueShotRequest(
                job_id="job-it-noframes",
                shot=self._ambience_shot(),
                artifact=common.RenderArtifact(shot_id="job-it-s000", video_path="/tmp/x.mp4"),
                attempt=1,
            )
        )
        assert resp.degraded is True
        assert resp.feedback.passed is False
        assert resp.feedback.source == common.FEEDBACK_SOURCE_SYSTEM

    def test_revise_shot_applies_human_feedback(self, grpc_stub, output_dir: Path) -> None:
        """人工意见回灌后必须重新渲染并自动复审。"""
        resp = grpc_stub.ReviseShot(
            pb.ReviseShotRequest(
                job_id="job-it-revise",
                shot=self._ambience_shot(),
                human_comment="标题太重了，改成更简洁的一句话",
                attempt=2,
                output_dir=str(output_dir),
            )
        )
        assert resp.artifact.video_path
        assert Path(resp.artifact.video_path).is_file()
        assert resp.feedback.model

    def test_generate_shot_with_missing_engine_is_non_retryable(
        self, grpc_stub, output_dir: Path
    ) -> None:
        """缺引擎必须映射成 FAILED_PRECONDITION，而不是 UNAVAILABLE。

        映射成 UNAVAILABLE 会让 Go 侧 Asynq 反复重试一个**永远不可能成功**的调用，
        既浪费资源又污染告警。
        """
        # 只有当本机真的没有 manim 时，这条才是有意义的断言。
        health = grpc_stub.Health(common.HealthRequest())
        if any(c == "engine:manim=ok" for c in health.capabilities):
            pytest.skip("本机已安装 Manim，无法验证缺引擎路径")

        with pytest.raises(grpc.RpcError) as exc_info:
            grpc_stub.GenerateShot(
                pb.GenerateShotRequest(
                    job_id="job-it-noengine",
                    shot=common.ShotSpec(
                        shot_id="s0", index=0, tag=common.SCENE_TAG_MATH,
                        engine=common.RENDER_ENGINE_MANIM, duration_sec=3.0,
                    ),
                    attempt=1,
                    output_dir=str(output_dir),
                )
            )
        assert exc_info.value.code() == grpc.StatusCode.FAILED_PRECONDITION


# ===========================================================================
# 降级
# ===========================================================================


class TestStyleGuideFallback:
    """坏的 style_guide_json 必须降级而不是让整个请求失败。"""

    def test_malformed_style_guide_still_succeeds(self, grpc_stub) -> None:
        resp = grpc_stub.PlanScript(
            pb.PlanScriptRequest(
                job_id="job-it-style",
                raw_script="测试风格降级。" * 10,
                target_duration_sec=30,
                style_guide_json="{ 这不是合法 JSON",
            )
        )
        assert len(resp.shots) > 0

    def test_out_of_range_style_guide_still_succeeds(self, grpc_stub) -> None:
        resp = grpc_stub.PlanScript(
            pb.PlanScriptRequest(
                job_id="job-it-style2",
                raw_script="测试风格降级二。" * 10,
                target_duration_sec=30,
                style_guide_json='{"min_font_size": 99999, "aspect_ratio": "离谱"}',
            )
        )
        assert len(resp.shots) > 0
