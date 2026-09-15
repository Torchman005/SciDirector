#!/usr/bin/env python
"""gRPC 冒烟测试客户端（手动联调用）。

用途：在不启动 Go 层的情况下，直接验证 Python AI 服务的 gRPC 契约。

它**自动识别阶段**：读 Health 的 capabilities，
* 有 ``pipeline`` -> 阶段二，跑真实链路（plan -> 流式流水线 -> 单镜头渲染）；
* 没有       -> 阶段一，验证四个 RPC 返回 ``UNIMPLEMENTED``。

这样同一个脚本在阶段一与阶段二都有意义，不必维护两份 ——
而且"该返回未实现时返回了别的东西"与"该实现时却返回未实现"都能被发现。

用法：
    python -m scidirector_ai.main     # 先启动服务
    python scripts/smoke-grpc.py
    python scripts/smoke-grpc.py --addr 127.0.0.1:50051 --duration 30

退出码：0 = 全部符合预期；1 = 有不符合预期的项；2 = 环境不满足。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "ai"))

import grpc  # noqa: E402

from scidirector_ai.pb import _PB_ROOT  # noqa: E402,F401 - 导入即完成 sys.path 注入

from scidirector.v1 import ai_service_pb2 as pb  # noqa: E402
from scidirector.v1 import ai_service_pb2_grpc as pb_grpc  # noqa: E402
from scidirector.v1 import common_pb2 as common  # noqa: E402

#: 阶段一预期为 UNIMPLEMENTED 的 RPC。
_PHASE1_UNIMPLEMENTED = ("RunPipeline", "GenerateShot", "CritiqueShot", "ReviseShot")

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


# ---------------------------------------------------------------------------
# 阶段一：验证"未实现"这一预期
# ---------------------------------------------------------------------------


def call_unimplemented(stub, checker: Checker, name: str) -> None:
    """断言某个 RPC 当前返回 UNIMPLEMENTED。"""
    print(f"\n[?] {name}（阶段一预期 UNIMPLEMENTED）")
    try:
        if name == "RunPipeline":
            list(stub.RunPipeline(pb.RunPipelineRequest(job_id="smoke", raw_script="x" * 20)))
        elif name == "GenerateShot":
            stub.GenerateShot(pb.GenerateShotRequest(
                job_id="smoke",
                shot=common.ShotSpec(shot_id="s0", index=0, tag=common.SCENE_TAG_MATH),
                attempt=1,
            ))
        elif name == "CritiqueShot":
            stub.CritiqueShot(pb.CritiqueShotRequest(
                job_id="smoke",
                shot=common.ShotSpec(shot_id="s0", index=0),
                artifact=common.RenderArtifact(shot_id="s0", video_path="/tmp/x.mp4"),
                attempt=1,
            ))
        elif name == "ReviseShot":
            stub.ReviseShot(pb.ReviseShotRequest(
                job_id="smoke",
                shot=common.ShotSpec(shot_id="s0", index=0),
                human_comment="字号放大", attempt=2,
            ))
        checker.fail(name, "调用成功返回了——阶段一预期应抛 UNIMPLEMENTED")
    except grpc.RpcError as exc:
        if exc.code() == grpc.StatusCode.UNIMPLEMENTED:
            checker.ok(name, "UNIMPLEMENTED（符合阶段一预期）")
        else:
            checker.fail(name, f"状态码为 {exc.code()}，预期 UNIMPLEMENTED")


# ---------------------------------------------------------------------------
# 阶段二：验证真实链路
# ---------------------------------------------------------------------------


def check_health(stub, checker: Checker) -> bool:
    """返回 True 表示流水线已实现（阶段二）。"""
    print("\n[1] Health")
    resp = stub.Health(common.HealthRequest())
    checker.check("healthy 为真", resp.healthy is True)
    checker.check("返回版本号", bool(resp.version), resp.version)

    caps = list(resp.capabilities)
    checker.check("capabilities 非空", bool(caps), ", ".join(caps[:6]))
    checker.check("带工具链明细", any(c.startswith("tool:") for c in caps),
                  ", ".join(c for c in caps if c.startswith("tool:")))
    checker.check("带逐引擎可用性", len([c for c in caps if c.startswith("engine:")]) >= 4,
                  ", ".join(c for c in caps if c.startswith("engine:")))
    return "pipeline" in caps


def check_plan(stub, checker: Checker, duration: float) -> None:
    print("\n[2] PlanScript（脚本 -> 分镜表）")
    resp = stub.PlanScript(pb.PlanScriptRequest(
        job_id="smoke-plan",
        raw_script=_SAMPLE_SCRIPT,
        target_duration_sec=duration,
        style_guide_json='{"theme":"dark","min_font_size":36}',
    ))
    shots = list(resp.shots)
    checker.check("返回了分镜", len(shots) > 0, f"{len(shots)} 个分镜")

    total = sum(s.duration_sec for s in shots)
    low, high = duration * 0.9, duration * 1.1
    checker.check("时长预算落在 ±10% 内", low <= total <= high,
                  f"合计 {total:.2f}s，允许 {low:.1f}~{high:.1f}s")
    checker.check("序号连续且从 0 开始",
                  [s.index for s in shots] == list(range(len(shots))),
                  str([s.index for s in shots]))

    bad = [s.index for s in shots
           if not s.shot_id or s.engine == common.RENDER_ENGINE_UNSPECIFIED
           or s.tag == common.SCENE_TAG_UNSPECIFIED or not s.visual_brief
           or not (2.0 <= s.duration_sec <= 25.0)]
    checker.check("每个分镜满足硬约束", not bad, f"违规序号 {bad}")

    print("       分镜明细：")
    for s in shots:
        print(f"         #{s.index} tag={common.SceneTag.Name(s.tag):<18} "
              f"engine={common.RenderEngine.Name(s.engine):<26} "
              f"duration={s.duration_sec:6.2f}s  {s.visual_brief[:26]}")


def check_pipeline(stub, checker: Checker, duration: float, work_dir: Path) -> None:
    print("\n[3] RunPipeline（流式流水线 —— 会真实渲染，可能需要十几秒）")
    events = list(stub.RunPipeline(pb.RunPipelineRequest(
        job_id="smoke-pipeline",
        raw_script="从勾股定理出发，用面积法证明。" * 3,
        target_duration_sec=duration,
        max_attempts_per_shot=2,
        style_guide_json=json.dumps({"min_font_size": 36}),
    )))
    checker.check("产出事件流", len(events) > 0, f"{len(events)} 条事件")

    nodes = {e.node for e in events}
    for expected in ("plan", "code", "render", "critique", "pipeline"):
        checker.check(f"包含 {expected} 节点事件", expected in nodes)

    plan_events = [e for e in events if e.node == "plan"]
    payload_ok = False
    if plan_events and plan_events[0].payload_json:
        try:
            payload = json.loads(plan_events[0].payload_json)
            first = payload["shots"][0]
            # 跨语言约定：snake_case + **数值**枚举（Go 用 encoding/json 解到 pb.ShotSpec）
            payload_ok = (
                "shot_id" in first and "visual_brief" in first
                and isinstance(first["tag"], int) and isinstance(first["engine"], int)
            )
        except (KeyError, IndexError, json.JSONDecodeError, TypeError):
            payload_ok = False
    checker.check("plan 事件带合规的分镜表 payload_json", payload_ok)

    statuses = {e.status for e in events}
    checker.check("有镜头通过审查", common.SHOT_STATUS_APPROVED in statuses)
    checker.check("事件带进度", all(0.0 <= e.progress <= 1.0 for e in events))
    checker.check("事件带时间戳", all(e.ts_unix_ms > 0 for e in events))

    # 注意 render 与 critique 事件会带**同一个** artifact（审查事件要引用
    # 它审的是哪一版产物），因此这里必须按路径去重再计数。
    paths = {e.artifact.video_path for e in events if e.HasField("artifact") and e.artifact.video_path}
    if paths:
        checker.check("渲染产出了视频文件（按路径去重）",
                      all(Path(p).is_file() and Path(p).stat().st_size > 0 for p in paths),
                      f"{len(paths)} 个不同产物")
    else:
        checker.fail("渲染产物", "事件流里没有任何 artifact")


def check_generate_shot(stub, checker: Checker, work_dir: Path) -> None:
    print("\n[4] GenerateShot（单镜头渲染 —— 走最可靠的氛围路径）")
    resp = stub.GenerateShot(pb.GenerateShotRequest(
        job_id="smoke-shot",
        shot=common.ShotSpec(
            shot_id="smoke-shot-s000", index=0,
            narration="开场：提出问题。", visual_brief="标题淡入",
            tag=common.SCENE_TAG_AMBIENCE, engine=common.RENDER_ENGINE_STOCK,
            duration_sec=2.0,
        ),
        attempt=1,
        output_dir=str(work_dir),
    ))
    checker.check("调用成功", resp.success is True, resp.error)
    checker.check("产出视频路径", bool(resp.artifact.video_path))
    if resp.artifact.video_path:
        path = Path(resp.artifact.video_path)
        checker.check("视频文件存在且非空", path.is_file() and path.stat().st_size > 0,
                      f"{path.stat().st_size if path.is_file() else 0} 字节")
    checker.check("时长有效", resp.artifact.duration_sec > 0,
                  f"{resp.artifact.duration_sec:.2f}s")
    checker.check("分辨率有效", resp.artifact.width > 0 and resp.artifact.height > 0,
                  f"{resp.artifact.width}x{resp.artifact.height}")


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="SciDirector gRPC 冒烟测试")
    parser.add_argument("--addr", default="127.0.0.1:50051", help="gRPC 地址")
    parser.add_argument("--duration", type=float, default=30.0, help="目标总时长（秒）")
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
        phase2 = check_health(stub, checker)
        print(f"\n>> 识别为{'阶段二（流水线已实现）' if phase2 else '阶段一（仅骨架）'}")

        check_plan(stub, checker, args.duration)

        if phase2:
            with tempfile.TemporaryDirectory(prefix="scid-smoke-") as tmp:
                work = Path(tmp)
                check_pipeline(stub, checker, args.duration, work)
                check_generate_shot(stub, checker, work)
        else:
            for name in _PHASE1_UNIMPLEMENTED:
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
