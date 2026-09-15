#!/usr/bin/env python
"""WebSocket 反馈闭环冒烟测试（手动联调用）。

验证四件事：
    1. 连接后立刻收到 ``snapshot``（任务快照 + 历史事件重放）；
    2. ``ping``/``pong`` 应用层心跳可用；
    3. ``resync`` 能按 ``after_id`` 增量补齐事件（断线续传）；
    4. 新提交任务的状态迁移能**实时**推送到已连接的客户端。

依赖：``websocket-client``（或 ``websockets``）。两者都不是本项目的运行依赖，
只用于手动联调；缺失时脚本会给出明确提示。

用法：
    # 先启动 Go API（见 README「快速开始」）
    python scripts/smoke-ws.py
    python scripts/smoke-ws.py --api http://127.0.0.1:8080

退出码：0 = 全部通过；1 = 有失败项；2 = 环境不满足（依赖缺失 / 连接失败）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

try:
    import websocket  # websocket-client
except ImportError:  # pragma: no cover - 环境相关
    print("[FATAL] 缺少依赖 websocket-client，请先 pip install websocket-client")
    sys.exit(2)

_SAMPLE_SCRIPT = (
    "从勾股定理出发，先用面积法给出证明，"
    "再用三组数据说明它在工程测量中的应用，最后总结其推广形式。"
)


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))
        if not ok:
            self.failures.append(name)


def post_json(api: str, path: str, payload: dict) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        api + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def recv_json(ws, timeout: float = 10.0) -> dict:
    """读取一条 JSON 消息；用 deadline 兜住"服务端不推送"的情况。"""
    ws.settimeout(timeout)
    return json.loads(ws.recv())


def main() -> int:
    parser = argparse.ArgumentParser(description="SciDirector WebSocket 冒烟测试")
    parser.add_argument("--api", default="http://127.0.0.1:8080", help="Go API 基址")
    parser.add_argument("--duration", type=float, default=30.0, help="目标总时长（秒）")
    args = parser.parse_args()

    ws_base = args.api.replace("https://", "wss://").replace("http://", "ws://")
    checker = Checker()

    # --- 准备：先建一个任务，保证历史事件非空 -------------------------------
    print("[准备] 提交一个任务以产生历史事件")
    try:
        created = post_json(args.api, "/api/v1/generate",
                            {"raw_script": _SAMPLE_SCRIPT, "target_duration_sec": args.duration})
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"[FATAL] 无法访问 {args.api}：{exc}")
        print("        请先启动 Go API（make dev-api）")
        return 2
    job_id = created["data"]["job_id"]
    print(f"       job_id = {job_id}")
    time.sleep(2)

    # --- 1) snapshot 重放 ---------------------------------------------------
    print("\n[1/4] 连接并接收 snapshot（加入即重放历史）")
    try:
        ws = websocket.create_connection(f"{ws_base}/ws/jobs/{job_id}", timeout=10)
    except Exception as exc:  # noqa: BLE001
        print(f"[FATAL] WebSocket 连接失败：{exc}")
        return 2
    msg = recv_json(ws)
    checker.check("首包类型为 snapshot", msg.get("type") == "snapshot", str(msg.get("type")))
    data = msg.get("data") or {}
    checker.check("snapshot 带任务体", isinstance(data.get("job"), dict),
                  f"job.status={data.get('job', {}).get('status')}")
    events = data.get("events") or []
    checker.check("snapshot 带历史事件", len(events) >= 2, f"{len(events)} 条")
    checker.check(
        "事件按时间升序且序号递增",
        [e["event_id"] for e in events] == sorted(e["event_id"] for e in events),
        str([e["event_id"] for e in events]),
    )
    checker.check("snapshot 带 seq（用于断线续传）", isinstance(msg.get("seq"), int), str(msg.get("seq")))

    # --- 2) ping/pong -------------------------------------------------------
    print("\n[2/4] 应用层心跳")
    ws.send(json.dumps({"type": "ping"}))
    pong = recv_json(ws)
    checker.check("ping -> pong", pong.get("type") == "pong", str(pong.get("type")))

    # --- 3) resync ----------------------------------------------------------
    print("\n[3/4] resync 增量补齐（after_id 之后的事件）")
    after = events[0]["event_id"] if events else 0
    ws.send(json.dumps({"type": "resync", "after_id": after}))
    got = recv_json(ws)
    checker.check("返回 events 类型", got.get("type") == "events", str(got.get("type")))
    ids = [e["event_id"] for e in (got.get("data") or [])]
    checker.check("只返回 after_id 之后的事件", all(i > after for i in ids), f"after={after} -> {ids}")

    # --- 4) 实时推送 --------------------------------------------------------
    print("\n[4/4] 新任务的状态迁移实时推送")
    created2 = post_json(args.api, "/api/v1/generate",
                         {"raw_script": _SAMPLE_SCRIPT, "target_duration_sec": args.duration,
                          "style_guide": {"min_font_size": 36}})
    job2 = created2["data"]["job_id"]
    print(f"       新 job_id = {job2}（另开一条连接观察）")

    ws2 = websocket.create_connection(f"{ws_base}/ws/jobs/{job2}", timeout=15)
    recv_json(ws2)  # 丢弃 snapshot
    live: list[dict] = []
    deadline = time.time() + 15
    while time.time() < deadline and len(live) < 3:
        try:
            frame = recv_json(ws2, timeout=max(deadline - time.time(), 0.5))
        except Exception:  # noqa: BLE001 - 超时即停止收集
            break
        if frame.get("type") == "event":
            live.append(frame["data"])

    checker.check("收到实时事件推送", len(live) > 0, f"{len(live)} 条")
    for e in live:
        print(f"         seq={e['event_id']} node={str(e.get('node')):<9} "
              f"status={e.get('status') or '-':<12} {(e.get('message') or '')[:52]}")

    ws2.close()
    ws.close()

    print("\n" + "=" * 64)
    if checker.failures:
        print(f"结果：{len(checker.failures)} 项失败 -> {checker.failures}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
