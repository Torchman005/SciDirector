#!/usr/bin/env python3
"""在真实浏览器里验证「一次生成请求的完整 span 树能在 Grafana 上看到」。

## 为什么必须用浏览器验，而不是查 API

阶段五的验收标准是「一次生成请求能在 Grafana 上看到完整的 span 树与耗时分解」。
这句话的主语是 **Grafana**，不是 Tempo 的 HTTP API。

这两者不是一回事，而且差别是**真实存在**的：现代 Grafana 里 Tempo 数据源的
TraceQL 搜索是**前端插件在浏览器里**直接打数据源代理执行的，不走 `/api/ds/query`
（那里只认 `traceId` 等少数后端查询类型）。也就是说 —— 后端 API 通不通，
**证明不了面板能渲染**。我一开始正是拿 `/api/ds/query` 去试，得到
`unsupported query type: 'traceql'`，差点得出「配置坏了」的错误结论。

因此本脚本做两件事：
  1. 在浏览器里打开看板与链路视图，**断言 DOM 里真的出现了三个服务的 span**；
  2. 截图存档（`--out-dir`），供人复核。

## 判定依据是 DOM 文本而不是像素

模型无法阅读图片，所以「看截图」不能作为断言。断言文本则可以被程序稳定判定，
截图退化为给人看的辅助证据 —— 两者都给，但只有文本参与判定。

用法：
    python scripts/verify-observability.py --trace-id <32位hex>
    python scripts/verify-observability.py            # 自动挑最近一条含 generate 的链路
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

GRAFANA = os.environ.get("SCID_GRAFANA_URL", "http://127.0.0.1:3000")
TEMPO = os.environ.get("SCID_TEMPO_URL", "http://127.0.0.1:3200")
DASHBOARD_UID = "scidirector-overview"

#: 三个服务名。缺任何一个都说明链路在中间断了 —— 那正是本脚本要抓的缺陷。
EXPECTED_SERVICES = ("scid-api", "scid-worker", "scidirector-ai")


def log(msg: str) -> None:
    print(f"[verify-obs] {msg}", flush=True)


def _find_chrome() -> str:
    """找一个可用的 Chromium（与 scripts/smoke-web.py 同一套发现顺序）。"""
    if os.environ.get("SCID_CHROME"):
        return os.environ["SCID_CHROME"]
    home = os.path.expanduser("~")
    for pat in (
        f"{home}/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
        "/root/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
        "/home/*/data/*/playwright-browsers/chromium-*/chrome-linux64/chrome",
    ):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    raise SystemExit("找不到可用的 Chromium：请设置 SCID_CHROME 指向浏览器可执行文件")


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=20) as resp:  # noqa: S310 - 本机固定地址
        return json.loads(resp.read().decode("utf-8"))


def _search_url(extra: str = "") -> str:
    """构造 Tempo 的 search 链接。

    **必须显式给 start/end。** 不给时 Tempo 会用一个很短的默认窗口
    （实测十几分钟），于是「刚跑过的任务」很快就搜不到了 ——
    而现象是「Tempo 里没有链路」，看起来像采集坏了，实际只是查询窗口的问题。
    我第一版就没给，脚本因此变成「跑完头几分钟能用、之后必失败」的假故障。
    """
    now = int(time.time())
    q = f"start={now - 3 * 3600}&end={now}&limit=50"
    return f"{TEMPO}/api/search?{q}" + (f"&{extra}" if extra else "")


def pick_trace_id() -> str:
    """挑一条最近、且含全部三个服务的链路。

    不直接用 search 返回的第一条：那条可能只是一次 /healthz 调用，
    它当然只有两个服务 —— 用它会得到一个「链路不全」的假失败。
    """
    data = _get_json(_search_url(f"tags={urllib.parse.quote('service.name=scid-api')}"))
    candidates = [
        t["traceID"]
        for t in (data.get("traces") or [])
        if "generate" in (t.get("rootTraceName") or "") or "generate" in (t.get("rootTraceName") or "")
    ]
    if not candidates:
        raise SystemExit(
            "Tempo 里没有找到 /api/v1/generate 的链路。"
            "先跑一次生成请求，并确认 SCID_OTEL_ENDPOINT 指向 collector。"
        )
    for trace_id in candidates:
        tree = _trace_services(trace_id)
        if set(EXPECTED_SERVICES).issubset(tree):
            return trace_id
    raise SystemExit(
        f"找到 {len(candidates)} 条 generate 链路，但没有一条同时包含 {EXPECTED_SERVICES}。"
        "这通常意味着**链路在某个环节断了**（常见：队列载荷没带 traceparent）。"
    )


def _trace_services(trace_id: str) -> set[str]:
    """从 Tempo 取一条链路里出现过的服务名集合。

    trace ID 用**补零后的 32 位**去查：Tempo 的 search 接口会返回**去掉前导零**的
    ID（实测 32 位里的首位 0 会被省掉），拿它跟日志里的 trace_id 做字符串比对会失败，
    而按 ID 查又是能查到的 —— 是个很容易误判成「链路丢了」的坑。
    """
    padded = trace_id.rjust(32, "0")
    data = _get_json(f"{TEMPO}/api/traces/{padded}")
    services: set[str] = set()
    for batch in data.get("batches", []):
        for attr in batch.get("resource", {}).get("attributes", []):
            if attr.get("key") == "service.name":
                services.add(list(attr.get("value", {}).values())[0])
    return services


def _trace_spans(trace_id: str) -> list[tuple[str, float]]:
    """返回 (span 名, 耗时毫秒) 列表 —— 第二份独立证据，不经过 Grafana。"""
    padded = trace_id.rjust(32, "0")
    data = _get_json(f"{TEMPO}/api/traces/{padded}")
    out: list[tuple[str, float]] = []
    for batch in data.get("batches", []):
        for scope in batch.get("scopeSpans", []):
            for span in scope.get("spans", []):
                start = int(span.get("startTimeUnixNano", 0))
                end = int(span.get("endTimeUnixNano", 0))
                out.append((span.get("name", "?"), (end - start) / 1e6))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-id", default=None, help="要验证的链路 ID（默认自动挑选）")
    parser.add_argument("--out-dir", default=".data/obs-shots", help="截图输出目录")
    parser.add_argument("--headful", action="store_true", help="显示浏览器窗口（本机调试用）")
    args = parser.parse_args()

    trace_id = args.trace_id or pick_trace_id()
    trace_id = trace_id.rjust(32, "0")
    services = _trace_services(trace_id)
    log(f"待验证链路 {trace_id}，服务：{sorted(services)}")
    if not set(EXPECTED_SERVICES).issubset(services):
        missing = sorted(set(EXPECTED_SERVICES) - services)
        log(f"！！链路缺少服务：{missing} —— 这是**断链**，不是渲染问题")
        return 2

    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    from playwright.sync_api import sync_playwright

    problems: list[str] = []

    # Grafana 的 Explore 链接格式**必须照抄它自己生成的**（schemaVersion=1 + panes），
    # 而且 `datasource` 要同时出现在 pane 与**每个 query 内部**。
    # 少了 query 内部那个，页面会静默退化成「没有选中数据源」的空白编辑器 ——
    # 不报错，只是什么都不显示，很容易被误判成「链路没采到」。
    # 我第一版就是照着自己猜的格式写，得到的正是这个空白页。
    # 另外 time 范围默认 1h，跨小时排查时改成 3h。
    panes = {
        "scid": {
            "datasource": "tempo",
            "queries": [
                {
                    "refId": "A",
                    "datasource": {"type": "tempo", "uid": "tempo"},
                    "queryType": "traceql",
                    # 一条查询覆盖三个服务：这样表格里会同时列出 HTTP 根 span、
                    # worker 的消费 span 与 Python 的图节点 span，配上各自的耗时 ——
                    # 也就是「一次请求的耗时分解」。
                    # 必须排除 /metrics：抓取端点每 5 秒一条，在 3 小时窗口里能把
                    # 表格的前 50 行全部占满，真正的业务链路根本挤不进来 ——
                    # 于是「链路视图里看不到 worker 的 span」看起来像断链，
                    # 实际只是被噪声挤掉了。这是本人踩过的坑。
                    "query": (
                        '{ resource.service.name =~ "scid-api|scid-worker|scidirector-ai"'
                        ' && name != "/metrics" }'
                    ),
                    "limit": 50,
                    # Spans Limit 默认只有 3 —— 那会把每条链路截到只剩根 span 附近几个，
                    # 于是「跨服务」这件事在表格里看不出来（worker 的消费 span 直接被截掉）。
                    # 验收要看的正是跨服务的那一段，所以必须调大。
                    "spansLimit": 30,
                    "tableType": "spans",
                }
            ],
            "range": {"from": "now-3h", "to": "now"},
        }
    }
    explore_url = (
        f"{GRAFANA}/explore?"
        + urllib.parse.urlencode(
            {"schemaVersion": "1", "panes": json.dumps(panes), "orgId": "1"}
        )
    )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=_find_chrome(),
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        try:
            page = browser.new_page(viewport={"width": 1700, "height": 1300})

            # --- 1) 看板页 ---
            page.goto(f"{GRAFANA}/d/{DASHBOARD_UID}", wait_until="load")
            page.wait_for_timeout(6000)
            dash_text = page.inner_text("body")
            if "生成链路" not in dash_text:
                problems.append("看板页没有渲染出标题（provisioning 可能没生效）")
            # 表格面板必须真的出了行：只断言标题的话，一个全是空面板的看板也能通过。
            dash_rows = re.findall(r"\b[0-9a-f]{16,32}\b", dash_text)
            if not dash_rows:
                problems.append("看板的链路表格没有渲染出任何 Trace ID 行")
            page.screenshot(path=os.path.join(out_dir, "dashboard.png"), full_page=True)
            log(f"看板：标题已渲染，表格行数（按 Trace ID 计）={len(dash_rows)}")

            # --- 2) 链路视图（验收标准的主语）---
            page.goto(explore_url, wait_until="load")
            # 链路视图是懒渲染的：等太短会得到空 DOM，
            # 从而把「渲染慢」误判成「没采到」——这两种情况要分清楚。
            page.wait_for_timeout(14000)
            trace_text = page.inner_text("body")
            page.screenshot(path=os.path.join(out_dir, "trace.png"), full_page=True)
            log(f"链路视图文本长度：{len(trace_text)}")

            for svc in EXPECTED_SERVICES:
                if svc not in trace_text:
                    problems.append(f"链路视图里没有出现服务 {svc}")
            for span_name in ("/api/v1/generate", "consume generate"):
                if span_name not in trace_text:
                    problems.append(f"链路视图里没有出现 span「{span_name}」")
            # 耗时列必须真的有数字 —— 只出现 span 名而没有任何时长，
            # 说明表格渲染不完整，验收标准里的「耗时分解」就没达成。
            if not re.search(r"\d+(\.\d+)?\s*(ms|s|µs)\b", trace_text):
                problems.append("链路视图里没有任何耗时数值")

            # --- 3) 程序化地再看一眼整棵树（浏览器之外的第二证据）---
            for svc in sorted(services):
                log(f"  Tempo 中该链路的服务：{svc}")
            spans = _trace_spans(trace_id)
            log(f"  Tempo 中该链路的 span 数：{len(spans)}（"
                + "、".join(sorted({n for n, _ in spans})[:6]) + " …）")
        finally:
            browser.close()

    print()
    if problems:
        log("发现问题：")
        for p in problems:
            log(f"  - {p}")
        return 1

    log(f"通过：看板与链路视图都在浏览器里渲染成功；截图见 {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
