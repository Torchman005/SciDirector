"""HTML 引擎（d3 / echarts / code_anim）现在也**在沙盒里**（阶段五）。

## 这个文件要证明的那一件事

这三个引擎渲染的是 **LLM 生成的页面**，页面里带着它的 JS。此前截图是在
**AI 服务进程内**直接起 Chromium 的，因此完全不经过 `SandboxRunner` ——
网络隔离、只读、seccomp 三块加固对它一律无效，生成的 JS 可以在一个有网络的
浏览器里自由外联。这是沙盒覆盖面上最大的一个洞。

搬进子进程之后，"浏览器也在沙盒里"这件事必须被**证明**，而不是靠看代码推断：
因此这里的核心用例是"页面里的 JS 真的连不出去"。判定方式是让页面自己发起
`fetch`，把结果写到它自己看得见的地方，再由测试读回来 ——
用浏览器**内部**的观察结果作证据，而不是在外面看网络。

## 两条必须成对出现的断言

- 隔离下：页面里的 `fetch` 必须失败；
- **不隔离**时：同一个页面必须能连出去（反向对照）。

少了后者，「连不出去」在一台本来就断网的机器上永远为真 —— 那时用例是绿的，
却只是证明了这台机器没网，而不是沙盒生效。
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from scidirector_ai.sandbox.runner import ResourceLimits, SandboxRunner

#: 可用浏览器。环境里装的 build 号常与 Playwright 期望的不一致（本机就是这样），
#: 因此优先用显式指定的那个；找不到就跳过 —— 但跳过原因要写清楚覆盖缺口。
CHROME = os.environ.get("SCID_CHROME", "")


def _find_chrome() -> str | None:
    if CHROME and Path(CHROME).is_file():
        return CHROME
    if os.environ.get("SCID_CHROME"):
        return None
    import glob

    for pat in (
        os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux/chrome"),
        "/root/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
        "/home/*/data/*/playwright-browsers/chromium-*/chrome-linux64/chrome",
    ):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    return None


CHROME_BIN = _find_chrome()
requires_chrome = pytest.mark.skipif(
    CHROME_BIN is None,
    reason="找不到可用的 Chromium（或 SCID_CHROME 指向的路径不存在），跳过 HTML 引擎沙盒验证",
)
requires_netns = pytest.mark.skipif(
    not __import__("scidirector_ai.sandbox.netns", fromlist=["x"]).network_isolation_available(),
    reason="本机不支持非特权用户命名空间，跳过 HTML 引擎沙盒验证",
)


#: 页面自己去发起 fetch，并把结果写进 document.title。
#:
#: 用 `document.title` 而不是画到 canvas 上：测试要能**读回**结果。
#: 走 CDP 读页面状态需要另一个连接，而这里用截图脚本已有的能力做不到 ——
#: 因此改用"页面自己把结论写到能被读的地方"这个思路：
#: 截图脚本会把 title 记进 PNG 的元数据？不，直接用一个更简单的通道：
#: 页面把结果写进 localStorage，截图脚本退出前打印出来。
_PROBE_JS = """
window.__ready = true;
window.__seek = (t) => {};
"""


def _egress_probe_script(tmp_path: Path, url: str) -> Path:
    """写一个"页面 + 探针"的组合脚本：截图之后把 fetch 的结果打印出来。

    这里**故意不复用** `html_capture.py`：那个脚本的职责是截图，
    不该为了测试长出"顺便报告网络状态"的能力。测试自己起一个最小脚本更清晰。
    """
    script = tmp_path / "probe.py"
    script.write_text(
        textwrap.dedent(
            f'''
            import sys
            from playwright.sync_api import sync_playwright

            html = {str(tmp_path / "page.html")!r}
            with sync_playwright() as pw:
                b = pw.chromium.launch(
                    executable_path={CHROME_BIN!r},
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
                )
                try:
                    page = b.new_page()
                    page.goto("file://" + html)
                    # 从**页面内部**发起请求：这正是"生成的 JS 能不能外联"的场景。
                    #
                    # 目标必须是**本机确实能连上**的地址（已实测 http://example.com 返回 200）。
                    # 我第一版用了 1.1.1.1 —— 那台机器在本机是被防火墙丢包的，
                    # 于是 fetch 一直挂着、`page.evaluate` 永不返回，
                    # 最后是**沙盒超时**把它杀掉（returncode=-9）：
                    # 一个"目标不可达"的环境问题被伪装成了沙盒故障。
                    #
                    # 页面侧还要自带超时（AbortController）：否则任何挂起的请求
                    # 都会拖到沙盒超时才结束，用例又慢又难判断。
                    # URL 必须**内联进 JS 字面量**：写成 Python 变量再在
                    # page.evaluate 里引用会得到 ReferenceError ——
                    # 那段代码是在浏览器里求值的，看不到 Python 的名字。
                    # （注释也必须留在 JS 之外：JS 不认 `#`。）
                    result = page.evaluate(
                        """async () => {{
                            const ac = new AbortController();
                            const timer = setTimeout(() => ac.abort(), 6000);
                            try {{
                                await fetch({url!r}, {{
                                    mode: "no-cors", cache: "no-store", signal: ac.signal,
                                }});
                                return "REACHABLE";
                            }} catch (e) {{
                                return "BLOCKED:" + e.name;
                            }} finally {{
                                clearTimeout(timer);
                            }}
                        }}"""
                    )
                    print("fetch", result)
                finally:
                    b.close()
            '''
        ),
        encoding="utf-8",
    )
    return script


def _write_page(tmp_path: Path) -> None:
    (tmp_path / "page.html").write_text(
        "<!doctype html><html><body><script>" + _PROBE_JS + "</script></body></html>",
        encoding="utf-8",
    )


@requires_chrome
def test_browser_js_cannot_reach_network_inside_sandbox(tmp_path) -> None:
    """**核心安全断言**：沙盒里浏览器内的 JS 连不出去（目标为公网地址）。

    反向对照由下面两条本机回环的用例承担（它们不依赖公网，因此不会跳过）。
    """
    _write_page(tmp_path)
    probe = _egress_probe_script(tmp_path, "http://example.com/")

    res = SandboxRunner("auto", "off", "off").run(
        [sys.executable, str(probe)], cwd=tmp_path, limits=ResourceLimits(timeout_sec=120)
    )

    assert res.network_isolation == "netns", "隔离未生效，这条用例就没有意义"
    assert res.returncode == 0, res.tail()
    assert "fetch BLOCKED" in res.stdout, (
        f"沙盒里的浏览器居然连出去了：{res.stdout!r}{res.tail()}"
    )


def _serve_once() -> tuple[str, "object"]:
    """在本机起一个极小的 HTTP 服务，返回 (url, server)。

    用它做反向对照，而不是依赖公网：公网可达性在本机是**时好时坏**的
    （实测同一个 example.com 有时 200、有时浏览器侧连不上），
    于是反向对照会时而跳过 —— 而"时跳过的对照"等于没有对照。
    本机回环完全可控，不受外网影响。
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_a) -> None:  # noqa: ANN002 - 静音访问日志
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}/", server


@requires_chrome
def test_browser_js_reaches_local_http_without_isolation(tmp_path) -> None:
    """**反向对照（确定性版）**：不隔离时，页面里的 fetch 必须成功。

    没有它，「fetch 被挡住」可能只是因为浏览器压根不能发请求
    （参数写错、页面加载失败……）—— 那时用例是绿的却什么都没证明。
    用本机回环而不是公网：公网可达性时好时坏，会让这条对照时而跳过。
    """
    url, server = _serve_once()
    try:
        _write_page(tmp_path)
        probe = _egress_probe_script(tmp_path, url)
        res = SandboxRunner("off", "off", "off").run(
            [sys.executable, str(probe)], cwd=tmp_path, limits=ResourceLimits(timeout_sec=120)
        )
    finally:
        server.shutdown()  # type: ignore[attr-defined]

    assert res.network_isolation == "none"
    assert res.returncode == 0, res.tail()
    assert "fetch REACHABLE" in res.stdout, f"不隔离时应当连得上：{res.stdout!r}{res.tail()}"


@requires_chrome
@requires_netns
def test_browser_js_cannot_reach_even_loopback_inside_sandbox(tmp_path) -> None:
    """隔离下连**本机回环**都不通 —— 说明命名空间是真的建起来了。

    与"公网连不上"相比，这条不依赖任何外部条件，因此可以稳定运行；
    两者合起来才说明「挡住的不是某一个地址，而是整条网络路径」。
    """
    url, server = _serve_once()
    try:
        _write_page(tmp_path)
        probe = _egress_probe_script(tmp_path, url)
        res = SandboxRunner("auto", "off", "off").run(
            [sys.executable, str(probe)], cwd=tmp_path, limits=ResourceLimits(timeout_sec=120)
        )
    finally:
        server.shutdown()  # type: ignore[attr-defined]

    assert res.network_isolation == "netns"
    assert res.returncode == 0, res.tail()
    assert "fetch BLOCKED" in res.stdout, f"隔离下回环不该通：{res.stdout!r}"


@requires_chrome
def test_sandboxed_capture_produces_frames(tmp_path) -> None:
    """搬进沙盒子进程之后，逐帧截图必须照常工作。

    这是"加固没把功能弄坏"的那一半证据 —— 上一轮的只读与 seccomp 也都各有一条。
    """
    from scidirector_ai.html_capture import main as capture_main

    frames = tmp_path / "frames"
    frames.mkdir()
    (tmp_path / "index.html").write_text(
        textwrap.dedent(
            """
            <!doctype html><html><body style="margin:0;background:#123">
            <canvas id="c" width="160" height="100"></canvas>
            <script>
            const ctx = document.getElementById('c').getContext('2d');
            window.__ready = true;
            window.__seek = (t) => {
              ctx.fillStyle = '#123'; ctx.fillRect(0, 0, 160, 100);
              ctx.fillStyle = '#7cf'; ctx.fillRect(4 + t * 30, 40, 30, 20);
            };
            </script></body></html>
            """
        ),
        encoding="utf-8",
    )

    rc = capture_main(
        [
            "--html", str(tmp_path / "index.html"),
            "--frames-dir", str(frames),
            "--frames", "3", "--fps", "2",
            "--width", "160", "--height", "100",
            "--start-sec", "0",
            "--executable", str(CHROME_BIN),
        ]
    )

    assert rc == 0
    produced = sorted(p.name for p in frames.glob("frame_*.png"))
    assert produced == ["frame_00000.png", "frame_00001.png", "frame_00002.png"], produced
    # 帧必须非空：Chromium 崩溃时有时留下 0 字节文件，"文件在"不等于"截到了"。
    for name in produced:
        assert (frames / name).stat().st_size > 0, f"{name} 是空文件"


@requires_chrome
def test_capture_reports_missing_seek_contract(tmp_path) -> None:
    """页面没有 `window.__seek` 时必须以**专用退出码 3** 报告。

    这是**内容问题**（提示词里给了模板，生成时没照做），与"浏览器坏了"完全不同：
    前者该回灌给编码智能体重写，后者该重试或转人工。
    退出码把两者分开，上层才能做出正确处置。
    """
    from scidirector_ai.html_capture import main as capture_main

    frames = tmp_path / "frames"
    frames.mkdir()
    (tmp_path / "bad.html").write_text(
        "<!doctype html><html><body><script>window.__ready = true;</script></body></html>",
        encoding="utf-8",
    )

    rc = capture_main(
        [
            "--html", str(tmp_path / "bad.html"),
            "--frames-dir", str(frames),
            "--frames", "1", "--fps", "1",
            "--width", "160", "--height", "100",
            "--executable", str(CHROME_BIN),
        ]
    )

    assert rc == 3, f"缺 window.__seek 应以退出码 3 报告，实际 {rc}"


@requires_chrome
def test_runner_wires_capture_into_the_sandbox(tmp_path) -> None:
    """经 runner 跑截图脚本时，网络隔离必须真的生效（防止"漏挂"）。

    只用 `html_capture.main()` 直接调用的用例**证明不了**沙盒生效 ——
    那正是改造前的形态。这条用例走 `SandboxRunner`，与生产路径一致。
    """
    frames = tmp_path / "frames"
    frames.mkdir()
    (tmp_path / "index.html").write_text(
        "<!doctype html><html><body><script>window.__ready=true;window.__seek=(t)=>{};</script></body></html>",
        encoding="utf-8",
    )
    script = Path(__file__).resolve().parents[1] / "scidirector_ai" / "html_capture.py"

    res = SandboxRunner("auto", "off", "off").run(
        [
            sys.executable, str(script),
            "--html", str(tmp_path / "index.html"),
            "--frames-dir", str(frames),
            "--frames", "1", "--fps", "1",
            "--width", "160", "--height", "100",
            "--executable", str(CHROME_BIN),
        ],
        cwd=tmp_path,
        limits=ResourceLimits(timeout_sec=120),
    )

    assert res.network_isolation == "netns"
    assert res.returncode == 0, res.tail()
    assert (frames / "frame_00000.png").stat().st_size > 0


# ---------------------------------------------------------------------------
# 渲染器必须**真的**走沙盒（而不是"改完忘了接上"）
# ---------------------------------------------------------------------------


class _RecordingRunner:
    """记录被调用的 argv，并返回一个成功结果 —— 不真的跑浏览器。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, argv, *, cwd, limits=None, env_extra=None, memory_probe=None):  # noqa: ANN001, ANN202
        from scidirector_ai.sandbox.runner import ExecResult

        self.calls.append(list(argv))
        return ExecResult(command=list(argv), returncode=0)


def test_renderer_capture_goes_through_the_runner(tmp_path) -> None:
    """`HtmlRenderer._capture` 必须**经 runner** 执行截图。

    这是本次修复的核心不变式：改造前它是在 AI 服务进程内直接起 Chromium 的，
    于是沙盒的三块加固对它一律无效。如果谁把这条链路改回进程内执行，
    这条用例会立刻变红 —— 而"浏览器能连出去"这种事，靠读代码是看不出来的。
    """
    from scidirector_ai.renderer import HtmlRenderer

    class _Settings:
        sandbox_timeout_sec = 30

    renderer = HtmlRenderer(_Settings(), engine="d3")
    frames = tmp_path / "frames"
    frames.mkdir()
    runner = _RecordingRunner()

    renderer._capture(
        html=tmp_path / "index.html",
        frames_dir=frames,
        total_frames=2,
        fps=2,
        request=type("R", (), {"width": 160, "height": 100})(),
        runner=runner,
        start_sec=0.0,
    )

    assert len(runner.calls) == 1, "截图必须经 runner 执行一次"
    argv = runner.calls[0]
    assert any("html_capture.py" in a for a in argv), f"应当调用截图脚本：{argv}"
    # 参数要能对上：帧数/尺寸/起始秒数都是渲染正确性的一部分。
    joined = " ".join(argv)
    assert "--frames 2" in joined and "--fps 2" in joined
    assert "--width 160" in joined and "--height 100" in joined


@requires_chrome
def test_full_html_render_produces_video_under_sandbox(tmp_path, monkeypatch) -> None:
    """**整条 HTML 引擎路径**在沙盒下出片：截图（沙盒子进程）-> ffmpeg 编码 -> ffprobe。

    前面几条分别验证了"截图脚本能跑"和"浏览器连不出去"，但没有任何一条证明
    **两者拼起来仍然能出片**。加固最常见的失败方式恰恰是"每一层单独看都对，
    合起来渲染不出来"（例如帧写到了沙盒里看不见的地方、或超时不够）。
    """
    monkeypatch.setenv("SCID_CHROME", str(CHROME_BIN))
    from scidirector_ai.config import Settings, reset_browser_probe_cache
    from scidirector_ai.renderer import HtmlRenderer, RenderRequest

    reset_browser_probe_cache()
    settings = Settings()
    renderer = HtmlRenderer(settings, engine="d3")
    ok, reason = renderer.available()
    assert ok, f"HTML 引擎应当可用（SCID_CHROME 已指向真实浏览器）：{reason}"

    request = RenderRequest(
        shot_id="job-x-s000",
        code=(
            "<!doctype html><html><body style='margin:0;background:#0B1020'>"
            "<canvas id='c' width='320' height='180'></canvas><script>"
            "const ctx=document.getElementById('c').getContext('2d');"
            "window.__ready=true;"
            "window.__seek=(t)=>{ctx.fillStyle='#0B1020';ctx.fillRect(0,0,320,180);"
            "ctx.fillStyle='#4F8CFF';ctx.fillRect(10+t*60,80,50,30);};"
            "</script></body></html>"
        ),
        output_dir=tmp_path,
        duration_sec=1.0,
        width=320,
        height=180,
        fps=4,
    )
    runner = SandboxRunner("auto", "off", "off")
    result = renderer.render(request, runner)

    video = Path(result.video_path)
    assert video.is_file() and video.stat().st_size > 0, "没有产出视频"
    # 时长要对得上：只截图不编码、或帧数算错都会在这里露出来。
    assert abs(result.media.duration_sec - 1.0) < 0.35, result.media
    assert result.media.width == 320 and result.media.height == 180, result.media
    assert result.render_cost_sec > 0
