"""gRPC 端到端集成测试。

为什么值得在阶段一就写：它一次性验证了四个最容易出错的接缝：
    1. protoc 生成物能否被正确导入（pb/__init__.py 的 sys.path shim）；
    2. proto <-> Pydantic 的枚举与字段转换是否正确；
    3. servicer 是否把「尚未实现」映射成 UNIMPLEMENTED 而不是 INTERNAL；
    4. 导演智能体的输出是否满足业务硬约束（时长预算）。

测试使用 **mock LLM**，因此不依赖网络与密钥，可以在 CI 中稳定运行。
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

grpc = pytest.importorskip("grpc", reason="未安装 grpcio，跳过 gRPC 集成测试")

from scidirector_ai.config import Settings  # noqa: E402
from scidirector_ai.grpc_server import build_server  # noqa: E402
from scidirector_ai.service import PipelineService  # noqa: E402

# pb/__init__.py 在导入时完成 sys.path 注入。
from scidirector.v1 import ai_service_pb2 as pb  # noqa: E402
from scidirector.v1 import ai_service_pb2_grpc as pbg  # noqa: E402
from scidirector.v1 import common_pb2 as common  # noqa: E402


def _free_port() -> int:
    """向操作系统要一个空闲端口。

    硬编码端口在 CI 上极易与并行任务冲突，导致随机失败 —— 那类失败最消耗信任。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def grpc_stub():
    """启动一个真实的 gRPC server，返回 (stub, port)。

    用 module 作用域：服务器启动/停止有固定开销，不必每个用例重启。
    """
    port = _free_port()
    settings = Settings(
        env="test",
        llm_provider="mock",
        ai_grpc_host="127.0.0.1",
        ai_grpc_port=port,
        grpc_max_workers=4,
    )
    server = build_server(settings, PipelineService(settings))
    server.start()

    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    grpc.channel_ready_future(channel).result(timeout=10)
    stub = pbg.AiDirectorServiceStub(channel)

    yield stub

    channel.close()
    server.stop(0)


class TestHealth:
    def test_health_reports_capabilities(self, grpc_stub) -> None:
        # HealthRequest/HealthResponse 定义在 common.proto（它们是跨服务共用的基础契约），
        # 因此这里必须用 common_pb2 而不是 ai_service_pb2。
        resp = grpc_stub.Health(common.HealthRequest())
        assert resp.healthy is True
        assert resp.version
        assert "plan" in resp.capabilities
        # 工具链明细也应当透出，便于编排层判断哪些标签不可渲染。
        assert any(c.startswith("tool:") for c in resp.capabilities)


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

        # 每个镜头都必须被正确路由到引擎。
        for shot in resp.shots:
            assert shot.shot_id, "shot_id 必须被派生"
            assert shot.engine != common.RENDER_ENGINE_UNSPECIFIED, "引擎必须由标签推导"
            assert shot.tag != common.SCENE_TAG_UNSPECIFIED, "标签必须被归一化"
            assert shot.visual_brief, "视觉意图不能为空（编码智能体依赖它）"
            assert 2.0 <= shot.duration_sec <= 25.0

    def test_outline_is_returned(self, grpc_stub) -> None:
        resp = grpc_stub.PlanScript(
            pb.PlanScriptRequest(job_id="job-it-2", raw_script="讲解梯度下降。" * 10, target_duration_sec=45)
        )
        assert resp.outline or resp.shots


class TestUnimplementedMapping:
    """阶段二之前，未实现的 RPC 必须返回 UNIMPLEMENTED。

    这一点很关键：如果返回 INTERNAL，Go 侧的 Asynq 会把它当作可重试的基础设施故障，
    反复重投同一个注定失败的调用，既浪费资源又污染告警。
    """

    def test_run_pipeline_is_unimplemented(self, grpc_stub) -> None:
        with pytest.raises(grpc.RpcError) as exc_info:
            list(grpc_stub.RunPipeline(pb.RunPipelineRequest(job_id="job-it-3", raw_script="x" * 20)))
        assert exc_info.value.code() == grpc.StatusCode.UNIMPLEMENTED

    def test_generate_shot_is_unimplemented(self, grpc_stub) -> None:
        with pytest.raises(grpc.RpcError) as exc_info:
            grpc_stub.GenerateShot(
                pb.GenerateShotRequest(
                    job_id="job-it-4",
                    shot=common.ShotSpec(shot_id="s0", index=0, tag=common.SCENE_TAG_MATH),
                    attempt=1,
                )
            )
        assert exc_info.value.code() == grpc.StatusCode.UNIMPLEMENTED

    def test_critique_shot_is_unimplemented(self, grpc_stub) -> None:
        with pytest.raises(grpc.RpcError) as exc_info:
            grpc_stub.CritiqueShot(
                pb.CritiqueShotRequest(
                    job_id="job-it-5",
                    shot=common.ShotSpec(shot_id="s0", index=0),
                    artifact=common.RenderArtifact(shot_id="s0", video_path="/tmp/x.mp4"),
                    attempt=1,
                )
            )
        assert exc_info.value.code() == grpc.StatusCode.UNIMPLEMENTED


class TestStyleGuideFallback:
    """坏的 style_guide_json 必须降级而不是让整个请求失败。"""

    def test_malformed_style_guide_still_succeeds(self, grpc_stub) -> None:
        resp = grpc_stub.PlanScript(
            pb.PlanScriptRequest(
                job_id="job-it-6",
                raw_script="测试风格降级。" * 10,
                target_duration_sec=30,
                style_guide_json="{ 这不是合法 JSON",
            )
        )
        assert len(resp.shots) > 0
