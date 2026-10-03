"""逐帧截图（在**沙盒子进程**里运行，见 renderer.HtmlRenderer）。

## 为什么单独成脚本

这三行 HTML 引擎（`d3` / `echarts` / `code_anim` / `motion`）渲染的是
**LLM 生成的代码**，而被渲染的页面里带着它的 JS。此前这段截图是在 **AI 服务
进程内**直接起 Chromium 的（`sync_playwright()`），因此**完全不经过
`SandboxRunner`** —— 也就是说另外几个引擎的网络隔离、只读、seccomp 全都覆盖不到它，
生成的 JS 可以在一个有网络的浏览器里跑。这是本项目沙盒覆盖面上最大的一个洞。

把截图搬进子进程、再经 `SandboxRunner` 执行，那三块加固就自动生效了。

## 为什么按「脚本路径」执行而不是 `-m`

`SandboxRunner` 会把子进程环境裁剪到白名单（不含 `PYTHONPATH`），cwd 又是工作目录；
`python -m 包.模块` 因此找不到包，表现为"截图全失败"。本脚本**只依赖标准库与
playwright**（都在 venv 里，与 cwd 无关），因此按绝对路径执行最稳。

## 为什么是"多页面并行 + JPEG"（v0.6.15 的性能改动）

实测：一个 **8 秒**镜头要 63 秒渲染，占整条流水线约 **70%** 的时间（12 次渲染
累计 759 秒 / 18.3 分钟）。这里是这么花掉的 —— 8s × 30fps = **240 帧**，
而每一帧原本是：

    page.evaluate(__seek) + page.evaluate(rAF) + page.screenshot(PNG 1080p)

1080p 的 PNG 编码又慢又大（每张 1~3MB），240 张写完还要 ffmpeg 再解码一遍。
两处改动各自独立、可叠加：

  1. **中间帧用 JPEG**（quality 90）。PNG 是无损的，但这一帧马上要被 x264 以
     crf20 重编码 —— 中间存无损几乎全被丢掉，却要付出 3~5 倍的编码时间与
     10 倍的磁盘写入。JPEG 90 的损失在这个链条里不可见。
  2. **多个页面并行截图**。`window.__seek(t)` 是**纯函数**：画面只由 t 决定，
     与调用顺序无关。因此把帧号切成 N 段交给 N 个页面并发截，得到的结果与
     串行逐帧**逐像素一致**，只是快得多。

确定性是刻意保住的：帧号仍然是全局连续的 `frame_%05d`，写哪个文件只由帧号决定，
与哪个 worker 去写无关；所以 ffmpeg 的输入序列与原实现完全相同。

## 它不做的事

不生成 HTML、不编码视频、不校验产物 —— 那些仍在 `renderer.py` 里。
这里只做"把页面按 `window.__seek(t)` 逐帧截下来"，边界越窄越不容易与主流程漂移。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 必须先做的 sys.path 清理 —— 顺序是**语义的一部分**，不要把它挪到下面去
# ---------------------------------------------------------------------------
# 以**脚本路径**执行时，`sys.path[0]` 是本文件所在目录 —— 而这个目录里有一个
# `logging.py`（本项目自己的结构化日志模块）。它会**遮蔽标准库的 `logging`**：
# 任何 `import logging` 都会拿到我们的模块，报
# `partially initialized module 'logging' has no attribute 'Formatter'`。
# 这个错误信息完全指不到真正的原因（目录遮蔽）。
#
# 关键在于**清理必须发生在第一个会间接 import logging 的模块之前**：
# `asyncio` / `concurrent.futures` 都会把它拉进来。v0.6.15 给本文件加
# `import asyncio` 时就把这件事踩了一次 —— 报错看起来像 playwright 坏了，
# 实际是导入顺序。有测试用 `--help` 钉住这条（那条路径会走完整个 import）。
import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path if str(Path(p or ".").resolve()) != _HERE]

# 清理之后才能安全导入这些（它们都可能间接 import logging）。
import argparse  # noqa: E402
import asyncio  # noqa: E402
import os  # noqa: E402

#: 等页面自报就绪的上限。页面可能异步加载字体，给足时间；
#: 但必须有上限，否则一个坏页面会让整个镜头挂到沙盒超时。
READY_TIMEOUT_MS = 30_000

#: 默认并发页面数。
#:
#: 截图的开销主要在 Chromium 的光栅化与图像编码上，两者都吃 CPU，
#: 所以并发数的上限取 CPU 核数但**压到 8**：再往上，收益会被内存带宽、
#: 磁盘写入与页面本身的内存占用吃掉（每个 1080p 页面常驻数十 MB），
#: 而单镜头渲染本来就是与其他镜头并行的，不该把整机吃满。
DEFAULT_WORKERS = max(1, min(8, os.cpu_count() or 4))

#: 中间帧的 JPEG 质量。90 在"几乎看不出差别"与"显著更小更快"之间。
DEFAULT_JPEG_QUALITY = 90

#: 帧文件扩展名（与 renderer.encode_frames 的 pattern 必须一致）。
EXT_BY_FORMAT = {"jpeg": "jpg", "png": "png"}


def frame_name(index: int, fmt: str) -> str:
    """帧文件名。格式只影响扩展名，**编号始终是全局连续的**。"""
    return f"frame_{index:05d}.{EXT_BY_FORMAT[fmt]}"


def split_frames(total: int, workers: int) -> list[list[int]]:
    """把 ``[0, total)`` 切成至多 ``workers`` 段**连续**区间。

    为什么连续切分而不是轮转（``i % workers``）：同一个 worker 拿到的帧在时间上
    相邻，页面只需在相近的绘制状态间小步推进，浏览器的光栅缓存更容易命中；
    轮转会让每个页面都在时间轴上反复跳，反而更慢。

    返回值的**不变式**（有测试钉住）：各段不重叠、并集恰好是 ``[0, total)``、
    且保持升序。这条一旦破坏，表现是"成片缺帧或顺序错乱"，
    而 ffmpeg 对缺帧只会静默产出一个更短的视频。
    """
    total = max(int(total), 0)
    workers = max(int(workers), 1)
    if total == 0:
        return []
    workers = min(workers, total)
    size, extra = divmod(total, workers)
    chunks: list[list[int]] = []
    start = 0
    for w in range(workers):
        n = size + (1 if w < extra else 0)
        chunks.append(list(range(start, start + n)))
        start += n
    return chunks


async def _capture_worker(
    browser: object,
    *,
    uri: str,
    frames_dir: Path,
    chunk: list[int],
    fps: int,
    width: int,
    height: int,
    start_sec: float,
    fmt: str,
    quality: int,
) -> tuple[bool, bool, str]:
    """一个页面负责一段帧。

    返回 ``(成功, 是否缺 __seek, 错误说明)``。缺 ``__seek`` 是**内容问题**
    （提示词给了模板、模型没照做），与"浏览器坏了"含义完全不同，
    必须能让上层区分开 —— 所以它单独占一个返回值，而不是混在错误字符串里。
    """
    page = await browser.new_page(  # type: ignore[attr-defined]
        viewport={"width": width, "height": height}, device_scale_factor=1
    )
    try:
        await page.goto(uri, wait_until="load")
        await page.wait_for_function("() => window.__ready !== false", timeout=READY_TIMEOUT_MS)

        if await page.evaluate("() => typeof window.__seek !== 'function'"):
            return False, True, ""

        shot_kwargs: dict[str, object] = {"path": ""}
        for index in chunk:
            # 局部重渲染时帧号从区间起点计：第 0 帧对应 start_sec 而不是整镜第 0 秒。
            # 漏加这个偏移是这里最典型的错误 —— 画面能出来、时长也对，
            # 但内容整体前移，且只有把片段拼回原片才看得出来。
            await page.evaluate("(t) => window.__seek(t)", start_sec + index / fps)
            # 等一帧 rAF，避免截到动画中间态。
            await page.evaluate("() => new Promise(r => requestAnimationFrame(() => r()))")
            shot_kwargs["path"] = str(frames_dir / frame_name(index, fmt))
            if fmt == "jpeg":
                await page.screenshot(type="jpeg", quality=quality, **shot_kwargs)  # type: ignore[arg-type]
            else:
                await page.screenshot(type="png", **shot_kwargs)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001 - playwright 异常类型繁多
        return False, False, f"{type(exc).__name__}: {exc}"
    finally:
        await page.close()
    return True, False, ""


async def _capture_async(
    *,
    html: Path,
    frames_dir: Path,
    total_frames: int,
    fps: int,
    width: int,
    height: int,
    start_sec: float,
    fmt: str,
    quality: int,
    workers: int,
    executable: str,
) -> tuple[bool, bool, str]:
    from playwright.async_api import async_playwright

    launch_kwargs: dict[str, object] = {
        # 容器里需要这些参数：没有 /dev/shm 与沙盒权限时 Chromium 会直接崩。
        "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    }
    if executable:
        # 仅用于测试与本机联调：环境里装的浏览器版本与当前 Playwright 期望的
        # build 号不一致时，走默认路径会报 "Executable doesn't exist"。
        # 那是环境问题，不该让沙盒这条链路没法验证。
        launch_kwargs["executable_path"] = executable

    chunks = split_frames(total_frames, workers)
    if not chunks:
        return True, False, ""

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)
        try:
            results = await asyncio.gather(
                *(
                    _capture_worker(
                        browser,
                        uri=html.resolve().as_uri(),
                        frames_dir=frames_dir,
                        chunk=chunk,
                        fps=fps,
                        width=width,
                        height=height,
                        start_sec=start_sec,
                        fmt=fmt,
                        quality=quality,
                    )
                    for chunk in chunks
                )
            )
        finally:
            await browser.close()

    for ok, missing_seek, err in results:
        if missing_seek:
            return False, True, ""
        if not ok:
            return False, False, err
    return True, False, ""


def capture(
    html: Path,
    frames_dir: Path,
    total_frames: int,
    fps: int,
    width: int,
    height: int,
    start_sec: float,
    executable: str = "",
    fmt: str = "jpeg",
    quality: int = DEFAULT_JPEG_QUALITY,
    workers: int = DEFAULT_WORKERS,
) -> int:
    """逐帧截图。返回 0 表示成功；非 0 时把原因写到 stderr。"""
    ok, missing_seek, err = asyncio.run(
        _capture_async(
            html=html,
            frames_dir=frames_dir,
            total_frames=total_frames,
            fps=fps,
            width=width,
            height=height,
            start_sec=start_sec,
            fmt=fmt,
            quality=quality,
            workers=workers,
            executable=executable,
        )
    )
    if ok:
        return 0
    if missing_seek:
        print(
            "页面未定义 window.__seek(t)，无法逐帧渲染。"
            "HTML 渲染路径要求页面暴露 window.__seek = (t) => {...}",
            file=sys.stderr,
        )
        return 3
    print(f"截图失败：{err}", file=sys.stderr)
    return 1


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
    # 下面三个保留为参数（而不是写死）：性能改动必须能被 A/B 实测，
    # 否则"快了"只是感觉。默认值就是生产用的那一组。
    parser.add_argument("--format", choices=sorted(EXT_BY_FORMAT), default="jpeg")
    parser.add_argument("--quality", type=int, default=DEFAULT_JPEG_QUALITY)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
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
            fmt=args.format,
            quality=args.quality,
            workers=args.workers,
        )
    except Exception as exc:  # noqa: BLE001 - 把原因写清楚，让上层能映射成渲染失败
        print(f"截图失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
