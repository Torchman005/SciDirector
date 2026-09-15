#!/usr/bin/env python
"""gRPC 冒烟测试客户端（手动联调用）。

用途：在不启动 Go 层的情况下，直接验证 Python AI 服务的 gRPC 契约是否正常。
它会依次调用五个 RPC，并按**阶段一的预期**校验返回：

    Health         -> 必须 healthy，且 capabilities 里带工具链明细
    PlanScript     -> 必须返回分镜表，且时长预算落在 ±10% 内
    RunPipeline    -> 阶段一预期 UNIMPLEMENTED（不可重试，Go 侧据此不重投）
    GenerateShot   -> 阶段一预期 UNIMPLEMENTED
    CritiqueShot   -> 阶段一预期 UNIMPLEMENTED

为什么把「预期未实现」也当作通过：阶段一交付的正是这套
「契约 + 骨架」，`UNIMPLEMENTED` 是**有意为之的正确行为**。
等到阶段二实现后，把 EXPECTED_UNIMPLEMENTED 清空即可复用本脚本。

用法：
    # 先启动服务：python -m scidirector_ai.main
    python scripts/smoke-grpc.py                      # 默认 127.0.0.1:50051
    python scripts/smoke-grpc.py --addr 127.0.0.1:50051 --duration 60

退出码：0 = 全部符合预期；1 = 有不符合预期的项。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让脚本可以从仓库任意位置运行。
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "ai"))

import grpc  # noqa: E402

from scidirector_ai.pb import _PB_ROOT  # noqa: E402,F401 - 导入即完成 sys.path 注入

from scidirector.v1 import ai_service_pb2 as pb  # noqa: E402
from scidirector.v1 import ai_service_pb2_grpc as pb_grpc  # noqa: E402
from scidirector.v1 import common_pb2 as common  # noqa: E402

#: 阶段一预期为 UNIMPLEMENTED 的 RPC。阶段二实现后清空此项。
EXPECTED_UNIMPLEMENTED = {"RunPipeline", "GenerateShot", "CritiqueShot", "ReviseShot"}

_SAMPLE_SCRIPT = (
    "从勾股定理出发，先用面积法给出证明，"
    "再用三组数据说明它在工程测量中的应用，最后总结其推广形式。"
)


class Checker:
    """收集检查结果，最后统一汇总。"""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def ok(self, name: str, detail: str = "") -> None:
        self.passed += 1
        print(f"  [PASS] {name}" + (f" -- {detail}" if detail else ""))

    def fail(self, name: str, detail: str) -> None:
        self.failures.append(f"{name}: {detail}")
        print(f"  [FAIL] {name} -- {detail}")

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.ok(name, detail)
        else:
            self.fail(name, detail or "断言失败")


def call_health(stub, checker: Checker) -> None:
    print("\n[1/5] Health")
    resp = stub.Health(common.HealthRequest())
    checker.check("healthy 为真", resp.healthy is True)
    checker.check("返回版本号", bool(resp.version), resp.version)
    checker.check("capabilities 非空", bool(resp.capabilities), ", ".join(resp.capabilities))
    checker.check(
        "capabilities 带工具链明细",
        any(c.startswith("tool:") for c in resp.capabilities),
        ", ".join(c for c in resp.capabilities if c.startswith("tool:")),
    )
    # 逐引擎的可用性也必须在 capabilities 里 —— 编排层据此提前知道
    # 「哪些标签当前渲染不了」，而不是等任务跑到那一步才失败。
    checker.check(
        "capabilities 带逐引擎可用性",
        sum(1 for c in resp.capabilities if c.startswith("engine:")) >= 4,
        ", ".join(c for c in resp.capabilities if c.startswith("engine:")),
    )


def call_plan(stub, checker: Checker, duration: float) -> None:
    print("\n[2/5] PlanScript（脚本 -> 分镜表）")
    resp = stub.PlanScript(
        pb.PlanScriptRequest(
            job_id="smoke-plan",
            raw_script=_SAMPLE_SCRIPT,
            target_duration_sec=duration,
            style_guide_json='{"theme":"dark","min_font_size":36}',
        )
    )
    shots = list(resp.shots)
    checker.check("返回了分镜", len(shots) > 0, f"{len(shots)} 个分镜")

    total = sum(s.duration_sec for s in shots)
    low, high = duration * 0.9, duration * 1.1
    checker.check(
        "时长预算落在 ±10% 内",
        low <= total <= high,
        f"合计 {total:.2f}s，允许 {low:.1f}~{high:.1f}s",
    )
    checker.check(
        "分镜序号连续且从 0 开始",
        [s.index for s in shots] == list(range(len(shots))),
        str([s.index for s in shots]),
    )

    bad = [
        s.index
        for s in shots
        if not s.shot_id or s.engine == common.RENDER_ENGINE_UNSPECIFIED
        or s.tag == common.SCENE_TAG_UNSPECIFIED or not s.visual_brief
        or not (2.0 <= s.duration_sec <= 25.0)
    ]
    checker.check("每个分镜都满足硬约束（ID/标签/引擎/视觉意图/时长区间）", not bad, f"违规序号 {bad}")

    print("       分镜明细：")
    for s in shots:
        print(
            f"         #{s.index} tag={common.SceneTag.Name(s.tag):<18} "
            f"engine={common.RenderEngine.Name(s.engine):<26} "
            f"duration={s.duration_sec:6.2f}s  {s.visual_brief[:28]}"
        )


def call_unimplemented(stub, checker: Checker, name: str) -> None:
    """断言某个 RPC 当前返回 UNIMPLEMENTED。"""
    print(f"\n[?/5] {name}（阶段一预期 UNIMPLEMENTED）")
    try:
        if name == "RunPipeline":
            list(stub.RunPipeline(pb.RunPipelineRequest(job_id="smoke", raw_script="x" * 20)))
        elif name == "GenerateShot":
            stub.GenerateShot(
                pb.GenerateShotRequest(
                    job_id="smoke",
                    shot=common.ShotSpec(shot_id="s0", index=0, tag=common.SCENE_TAG_MATH),
                    attempt=1,
                )
            )
        elif name == "CritiqueShot":
            stub.CritiqueShot(
                pb.CritiqueShotRequest(
                    job_id="smoke",
                    shot=common.ShotSpec(shot_id="s0", index=0),
                    artifact=common.RenderArtifact(shot_id="s0", video_path="/tmp/x.mp4"),
                    attempt=1,
                )
            )
        elif name == "ReviseShot":
            stub.ReviseShot(
                pb.ReviseShotRequest(
                    job_id="smoke",
                    shot=common.ShotSpec(shot_id="s0", index=0),
                    human_comment="字号放大",
                    attempt=2,
                )
            )
        checker.fail(name, "调用成功返回了——阶段一预期应抛 UNIMPLEMENTED")
    except grpc.RpcError as exc:
        if exc.code() == grpc.StatusCode.UNIMPLEMENTED:
            checker.ok(name, "UNIMPLEMENTED（符合阶段一预期）")
        else:
            checker.fail(name, f"状态码为 {exc.code()}，预期 UNIMPLEMENTED")


def main() -> int:
    parser = argparse.ArgumentParser(description="SciDirector gRPC 冒烟测试")
    parser.add_argument("--addr", default="127.0.0.1:50051", help="gRPC 地址")
    parser.add_argument("--duration", type=float, default=60.0, help="目标总时长（秒）")
    parser.add_argument("--timeout", type=float, default=30.0, help="单次调用超时（秒）")
    args = parser.parse_args()

    print(f"连接 {args.addr} …")
    channel = grpc.insecure_channel(args.addr)
    try:
        grpc.channel_ready_future(channel).result(timeout=10)
    except grpc.FutureTimeoutError:
        print(f"[FATAL] 无法连接 {args.addr}；请先启动 AI 服务：python -m scidirector_ai.main")
        return 2

    stub = pb_grpc.AiDirectorServiceStub(channel)
    checker = Checker()

    try:
        call_health(stub, checker)
        call_plan(stub, checker, args.duration)
        for name in sorted(EXPECTED_UNIMPLEMENTED):
            call_unimplemented(stub, checker, name)
    finally:
        channel.close()

    print("\n" + "=" * 64)
    if checker.failures:
        print(f"结果：{checker.passed} 项通过，{len(checker.failures)} 项失败")
        for item in checker.failures:
            print(f"  - {item}")
        return 1
    print(f"结果：全部 {checker.passed} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
