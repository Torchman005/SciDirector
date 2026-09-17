#!/usr/bin/env python3
"""C1 / C3 的浏览器端验证：真实 Chromium + 真实全栈。

为什么需要它：
`docs/ROADMAP.md` 的 C1/C3 一直标着「浏览器人工目视验证未做」，
而这两条恰恰是**最容易在纯单测里漏掉**的 ——
C2/C3 的全部逻辑都在前端归约（`stream.ts`）里，出问题的表现是
「偶尔少一条事件」「进度条退一格」，靠点页面几乎不可能稳定复现。

本脚本负责**可自动化的那一半**：数据通路、进度单调性、断网后能否补齐且不回跳，
并把关键节点截图下来供人工目视。它**不能**代替人看画面长得对不对 ——
所以产物里既有断言结果，也有截图。

用法（需要全栈已在跑：api:8080 / web:5173 / redis / rustfs）：
    python scripts/smoke-web.py --shots /tmp/scid-shots
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

# 浏览器可执行文件。默认会依次尝试：
#   1. SCID_CHROME 显式指定（最可靠）
#   2. Playwright 自己安装的 chromium（`playwright install chromium`）
#   3. 本机已存在的 Chrome for Testing
# 之所以要这个降级链：本机装不了 Playwright 的浏览器（其 CDN 不通），
# 但系统里已有可用的 Chrome for Testing，复用它即可。
def _find_chrome() -> str:
    if os.environ.get("SCID_CHROME"):
        return os.environ["SCID_CHROME"]
    import glob

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


CHROME = _find_chrome()
WEB = os.environ.get("SCID_WEB_URL", "http://127.0.0.1:5173")
API = os.environ.get("SCID_API_URL", "http://127.0.0.1:8080")

SCRIPT = (
    "从勾股定理出发，用面积法证明它，并给出一个生活中的例子说明它的用处。"
    "接着解释为什么它在测量与导航里如此重要。"
)


def log(msg: str) -> None:
    print(f"[smoke-web] {msg}", flush=True)


def ui_state(page) -> dict:
    """把页面上「用户能看到的」东西抓成一个结构，用于断言。"""
    return page.evaluate(
        """() => {
          const bar = document.querySelector('.progress-bar');
          const stat = document.querySelector('.row-stat');
          const rows = [...document.querySelectorAll('.shot-row')].map(r => ({
            idx: r.querySelector('.shot-index')?.textContent?.trim(),
            status: r.querySelector('.pill')?.textContent?.trim(),
          }));
          const conn = document.querySelector('.conn');
          return {
            progress: bar ? (parseInt(bar.style.width, 10) || 0) : null,
            statText: stat ? stat.innerText.replace(/\\s+/g, ' ').trim() : null,
            shots: rows,
            connText: conn ? conn.innerText.replace(/\\s+/g, ' ').trim() : null,
            hasJob: !!document.querySelector('.row-stat'),
          };
        }"""
    )


def server_state(job_id: str) -> dict:
    """直接问服务端要真值，用来判断页面有没有「补齐」。"""
    with urllib.request.urlopen(f"{API}/api/v1/jobs/{job_id}", timeout=10) as resp:
        data = json.load(resp)["data"]
    return {
        "status": data["job"]["status"],
        "progress": data["progress"],
        "stat": data["stat"],
        "shots": [
            {"index": s["index"], "tag": s["tag"], "status": s["status"]} for s in data["job"]["shots"]
        ],
    }


def submit(page, shots_dir: Path, tag: str) -> None:
    page.fill(".script-input", SCRIPT)
    page.click("button.btn-primary")
    page.wait_for_selector(".row-stat", timeout=20_000)
    page.screenshot(path=str(shots_dir / f"{tag}-01-submitted.png"), full_page=True)
    log(f"{tag}: 已提交，任务卡片出现")


def wait_terminal(page, job_id: str, timeout: float = 180.0) -> dict:
    """轮询到任务进入终态，同时记录「是否单调递增」。"""
    deadline = time.time() + timeout
    samples: list[int] = []
    last = -1
    regression = None
    while time.time() < deadline:
        st = ui_state(page)
        if st["progress"] is not None:
            if st["progress"] < last:
                regression = (last, st["progress"])
            last = max(last, st["progress"])
            samples.append(st["progress"])
        srv = server_state(job_id)
        if srv["status"] in {"COMPLETED", "PARTIAL", "FAILED"}:
            return {"samples": samples, "regression": regression, "server": srv}
        time.sleep(1.0)
    raise AssertionError(f"任务在 {timeout}s 内没有进入终态")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default="/tmp/scid-shots", help="截图输出目录")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument(
        "--api-restart-cmd",
        default="",
        help="形如 `docker compose` 或某个脚本，脚本会执行 `<cmd> down` 与 `<cmd> up` 来真正切断实时通道",
    )
    args = ap.parse_args()

    shots_dir = Path(args.shots)
    shots_dir.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROME,
            headless=not args.headed,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()
        page.on("console", lambda m: None)

        # ------------------------------------------------------------------
        # C1：提交脚本后，前端实时看到分镜逐个出现并推进状态
        # ------------------------------------------------------------------
        log("=== C1：实时出现与推进 ===")
        page.goto(WEB, wait_until="domcontentloaded")
        page.wait_for_selector(".script-input", timeout=20_000)
        page.screenshot(path=str(shots_dir / "c1-00-initial.png"), full_page=True)

        submit(page, shots_dir, "c1")

        # 抓「分镜出现 + 状态推进」的过程。
        #
        # 注意一个容易想当然的地方：**分镜不是逐个出现的**。
        # 导演智能体在 `plan` 节点一次产出整张分镜表，所以它们会一起出现；
        # 随后每个镜头各自推进状态（PENDING → GENERATING → APPROVED / AWAITING_HUMAN）。
        # 因此这里断言的是「出现 + 逐个推进」，而不是「逐个出现」。
        seen_status: dict[str, set[str]] = {}
        progress_trace: list[int] = []
        shot_count_trace: list[int] = []
        deadline = time.time() + 180
        while time.time() < deadline:
            st = ui_state(page)
            if st["progress"] is not None:
                progress_trace.append(st["progress"])
                shot_count_trace.append(len(st["shots"]))
                for s in st["shots"]:
                    seen_status.setdefault(s["idx"] or "?", set()).add(s["status"] or "?")
            srv = server_state_by_page(page)
            if srv and srv["status"] in {"COMPLETED", "PARTIAL", "FAILED"}:
                break
            time.sleep(0.4)

        page.screenshot(path=str(shots_dir / "c1-99-final.png"), full_page=True)

        if not shot_count_trace:
            failures.append("C1：始终没读到进度条，页面可能没进入任务视图")
        else:
            if max(shot_count_trace) < 1:
                failures.append("C1：整个过程中一个分镜都没渲染出来")
            if progress_trace != sorted(progress_trace):
                failures.append(f"C1：进度出现回退：{progress_trace}")
            # 注意：进度为 0 **不一定**是缺陷 —— 若这次脚本没拆出 AMBIENCE 镜头，
            # 本机缺 manim/d3 时所有镜头都会转人工，而进度 = 已通过/总数 合法地为 0。
            # 因此这里断言的是「确实推进过」，由下面的状态变化来证明。
            if max(progress_trace) == 0 and not any(len(v) >= 2 for v in seen_status.values()):
                failures.append("C1：进度恒为 0 且没有任何状态推进，流水线似乎没动")
            # 「推进状态」：至少有一个镜头经历过两种以上状态
            advanced = {k: v for k, v in seen_status.items() if len(v) >= 2}
            if not advanced:
                failures.append(
                    f"C1：没有任何镜头发生状态变化，只看到静态列表：{seen_status}"
                )
        log(f"C1 采样：分镜条数 {sorted(set(shot_count_trace))}，进度轨迹 {progress_trace}")
        log(f"C1 状态变化：{ {k: sorted(v) for k, v in seen_status.items()} }")

        # ------------------------------------------------------------------
        # C3：断网 10 秒后恢复，进度自动补齐且不回跳
        # ------------------------------------------------------------------
        log("=== C3：断网 10 秒后恢复 ===")
        # 按**文案**点「新建任务」，不要用 .btn-sm 这类样式类选择器 ——
        # 页面上有多个 btn-sm（顶部的「重连」也是一个），按样式点会点错，
        # 而且失败现象是「等不到脚本输入框」，很容易被误判成页面坏了。
        page.get_by_role("button", name="新建任务").click()
        page.wait_for_selector(".script-input", timeout=20_000)
        submit(page, shots_dir, "c3")

        # 等进度真的动起来再断网，否则测的是「还没开始就断了」
        for _ in range(30):
            if (ui_state(page)["progress"] or 0) > 0:
                break
            time.sleep(0.5)

        before = ui_state(page)
        log(f"C3 断网前：progress={before['progress']} conn={before['connText']!r}")

        # 断网演练。
        #
        # 关键事实（实测）：`context.set_offline(True)` **不会**断开已经建立的
        # WebSocket —— 连接指示器全程仍是「实时」，演练等于没做。
        # 因此这里优先用「停掉 api 进程 10 秒再拉起」来真正切断实时通道；
        # 这同时覆盖了代码里明确设计过的场景（重连退避的抖动就是为了
        # 「服务端重启时所有客户端同时涌上来」）。
        # 没有给 --api-restart-cmd 时退回 set_offline，并**如实报告**它没能断线。
        if args.api_restart_cmd:
            subprocess.Popen(["bash", "-c", args.api_restart_cmd + " down"], start_new_session=True).wait()
            log("C3：已切断 api（实时通道断开）")
        else:
            ctx.set_offline(True)
            log("C3：仅使用 set_offline —— 注意它通常不会断开已建立的 WS")

        time.sleep(10.0)  # 需求里明确写了「断网 10 秒」
        page.screenshot(path=str(shots_dir / "c3-01-offline.png"), full_page=True)
        offline = ui_state(page)
        log(f"C3 断网中：progress={offline['progress']} conn={offline['connText']!r}")

        if args.api_restart_cmd:
            subprocess.Popen(["bash", "-c", args.api_restart_cmd + " up"], start_new_session=True).wait()
            log("C3：api 已恢复")
        else:
            ctx.set_offline(False)
            if offline["connText"] and "实时" in offline["connText"]:
                failures.append(
                    "C3：断网期间连接指示器仍显示「实时」—— set_offline 没有断开已建立的 "
                    "WebSocket，本次演练并未真正断线（请用 --api-restart-cmd 做真断线）"
                )
        # 等重连（连接指示器恢复）——给它足够时间走完指数退避
        recovered = False
        deadline = time.time() + 90
        while time.time() < deadline:
            st = ui_state(page)
            if st["connText"] and "实时" in st["connText"]:
                recovered = True
                break
            time.sleep(1.0)
        page.screenshot(path=str(shots_dir / "c3-02-recovered.png"), full_page=True)
        if not recovered:
            failures.append(f"C3：恢复联网后 90s 内没有重连成功（conn={ui_state(page)['connText']!r}）")

        # 关键断言①：恢复后进度不得低于断网前（「进度不回跳」）
        after = ui_state(page)
        log(f"C3 恢复后：progress={after['progress']} conn={after['connText']!r}")
        if after["progress"] is not None and before["progress"] is not None:
            if after["progress"] < before["progress"]:
                failures.append(
                    f"C3：进度回跳 —— 断网前 {before['progress']}%，恢复后 {after['progress']}%"
                )

        # 关键断言②：「补齐」的**正面证据** —— 页面必须收敛到服务端真值。
        # 只断言「没回跳」是不够的：一个永远停在旧状态的页面同样不会回跳。
        c3_job = page_job_id(page)
        if not c3_job:
            failures.append("C3：读不到当前 job_id，无法核对是否补齐")
        else:
            deadline = time.time() + 90
            converged = False
            last_ui: dict = {}
            while time.time() < deadline:
                ui = ui_state(page)
                srv = server_state(c3_job)
                last_ui = ui
                if stat_matches(ui["statText"] or "", srv["stat"]) and ui["progress"] == round(
                    srv["progress"] * 100
                ):
                    converged = True
                    break
                time.sleep(1.0)
            page.screenshot(path=str(shots_dir / "c3-03-converged.png"), full_page=True)
            if not converged:
                failures.append(
                    f"C3：恢复联网后页面没有收敛到服务端真值（页面 {last_ui.get('statText')!r}，"
                    f"服务端 {srv['stat']}）—— 断线期间的事件没有被补齐"
                )
            else:
                log(f"C3 已补齐：页面与服务端一致 {srv['stat']}")

        browser.close()

    # ----------------------------------------------------------------------
    # 结果
    # ----------------------------------------------------------------------
    print()
    if failures:
        log("❌ 失败：")
        for f in failures:
            print(f"   - {f}")
        return 1
    log(f"✅ C1/C3 自动化断言全部通过；截图见 {shots_dir}")
    log("注意：截图只供**人工目视** —— 自动化能证明数据通路与单调性，")
    log("      证明不了「画面看起来对不对」，那一半仍然要人看。")
    return 0


def server_state_by_page(page) -> dict | None:
    """从页面里拿到当前 jobId，再问服务端要真值。"""
    job_id = page_job_id(page)
    if not job_id:
        return None
    try:
        return server_state(job_id)
    except Exception:
        return None


def page_job_id(page) -> str | None:
    """页面上展示的任务号（`任务 <code>job-xxx</code>`）。"""
    jid = page.evaluate(
        """() => {
          const c = document.querySelector('.card code');
          return c ? c.textContent.trim() : null;
        }"""
    )
    return jid if jid and jid.startswith("job-") else None


def stat_matches(stat_text: str, server_stat: dict) -> bool:
    """比较页面统计行与服务端统计。

    用**数字**比对，不比对中文标签 —— 标签是展示层的东西（`display.ts` 里），
    拿它做断言等于把一个纯展示的映射也钉进契约里，改文案就会误报。
    """
    import re

    def nums(pattern: str) -> int | None:
        m = re.search(pattern, stat_text)
        return int(m.group(1)) if m else None

    checks = {
        "approved": nums(r"已通过\s*(\d+)"),
        "awaiting_human": nums(r"待人工\s*(\d+)"),
        "failed": nums(r"失败\s*(\d+)"),
        "in_progress": nums(r"进行中\s*(\d+)"),
    }
    return all(v is not None and v == server_stat.get(k) for k, v in checks.items())


if __name__ == "__main__":
    sys.exit(main())
