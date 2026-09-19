"""逐帧截图（在**沙盒子进程**里运行，见 renderer.HtmlRenderer）。

## 为什么单独成脚本

这三行 HTML 引擎（`d3` / `echarts` / `code_anim`）渲染的是 **LLM 生成的代码**，
而被渲染的页面里带着它的 JS。此前这段截图是在 **AI 服务进程内**直接起 Chromium 的
（`sync_playwright()`），因此**完全不经过 `SandboxRunner`** ——
也就是说另外三个引擎的网络隔离、只读、seccomp 全都覆盖不到它，
生成的 JS 可以在一个有网络的浏览器里跑。这是本项目沙盒覆盖面上最大的一个洞。

把截图搬进子进程、再经 `SandboxRunner` 执行，那三块加固就自动生效了。

## 为什么按「脚本路径」执行而不是 `-m`

`SandboxRunner` 会把子进程环境裁剪到白名单（不含 `PYTHONPATH`），cwd 又是工作目录；
`python -m 包.模块` 因此找不到包，表现为"截图全失败"。本脚本**只依赖标准库与
playwright**（都在 venv 里，与 cwd 无关），因此按绝对路径执行最稳。

## 它不做的事

不生成 HTML、不编码视频、不校验产物 —— 那些仍在 `renderer.py` 里。
这里只做"把页面按 `window.__seek(t)` 逐帧截下来"，边界越窄越不容易与主流程漂移。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 以**脚本路径**执行时，`sys.path[0]` 是本文件所在目录 —— 而这个目录里有一个
# `logging.py`（本项目自己的结构化日志模块）。它会**遮蔽标准库的 `logging`**：
# playwright 内部的 `import logging` 会拿到我们的模块，报
# `partially initialized module 'logging' has no attribute 'Formatter'`。
# 这个错误信息完全指不到真正的原因（目录遮蔽），因此这里显式把它摘掉。
# （本模块只用标准库与 playwright，所以摘掉之后不需要任何本项目的东西。）
_HERE = str(Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path if str(Path(p or ".").resolve()) != _HERE]

#: 等页面自报就绪的上限。页面可能异步加载字体，给足时间；
#: 但必须有上限，否则一个坏页面会让整个镜头挂到沙盒超时。
READY_TIMEOUT_MS = 30_000


def capture(
    html: Path,
    frames_dir: Path,
    total_frames: int,
    fps: int,
    width: int,
    height: int,
    start_sec: float,
    executable: str = "",
) -> int:
    """逐帧截图。返回 0 表示成功；非 0 时把原因写到 stderr。"""
    from playwright.sync_api import sync_playwright

    launch_kwargs: dict[str, object] = {
        # 容器里需要这些参数：没有 /dev/shm 与沙盒权限时 Chromium 会直接崩。
        "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    }
    if executable:
        # 仅用于测试与本机联调：环境里装的浏览器版本与当前 Playwright 期望的
        # build 号不一致时，走默认路径会报 "Executable doesn't exist"。
        # 那是环境问题，不该让沙盒这条链路没法验证。
        launch_kwargs["executable_path"] = executable

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch_kwargs)  # type: ignore[arg-type]
        try:
            page = browser.new_page(
                viewport={"width": width, "height": height}, device_scale_factor=1
            )
            page.goto(html.resolve().as_uri(), wait_until="load")
            page.wait_for_function("() => window.__ready !== false", timeout=READY_TIMEOUT_MS)

            if page.evaluate("() => typeof window.__seek !== 'function'"):
                # 契约缺失是**内容问题**（提示词里给了模板，生成时没照做），
                # 与"浏览器坏了"完全不同，必须能让上层区分开。
                print(
                    "页面未定义 window.__seek(t)，无法逐帧渲染。"
                    "HTML 渲染路径要求页面暴露 window.__seek = (t) => {...}",
                    file=sys.stderr,
                )
                return 3

            for index in range(total_frames):
                # 局部重渲染时帧号从区间起点计：第 0 帧对应 start_sec 而不是整镜第 0 秒。
                # 漏加这个偏移是这里最典型的错误 —— 画面能出来、时长也对，
                # 但内容整体前移，且只有把片段拼回原片才看得出来。
                page.evaluate("(t) => window.__seek(t)", start_sec + index / fps)
                # 等一帧 rAF，避免截到动画中间态。
                page.evaluate("() => new Promise(r => requestAnimationFrame(() => r()))")
                page.screenshot(path=str(frames_dir / f"frame_{index:05d}.png"))
        finally:
            browser.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="沙盒内的逐帧截图")
    parser.add_argument("--html", required=True)
    parser.add_argument("--frames-dir", required=True)
    parser.add_argument("--frames", type=int, required=True)
    parser.add_argument("--fps", type=int, required=True)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--start-sec", type=float, default=0.0)
    parser.add_argument("--executable", default="")
    args = parser.parse_args(argv)

    try:
        return capture(
            html=Path(args.html),
            frames_dir=Path(args.frames_dir),
            total_frames=args.frames,
            fps=args.fps,
            width=args.width,
            height=args.height,
            start_sec=args.start_sec,
            executable=args.executable,
        )
    except Exception as exc:  # noqa: BLE001 - 把原因写清楚，让上层能映射成渲染失败
        print(f"截图失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
