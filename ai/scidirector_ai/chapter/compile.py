"""章节编译与渲染：受信任运行时 + 导演代码 → 可 seek 的整章 HTML → 成片。

导演代码只能调用 ``chapter_runtime.js`` 导出的 ``SC`` 函数；页面不加载任何外部资源。
渲染复用镜头路径的逐帧截图（``html_capture``），旁白音轨按时间轴的片头留白对齐。
"""
from __future__ import annotations

import asyncio
import json
import re
import math
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from ..html_capture import capture

#: 与参考成片一致的纸面配色；导演代码通过 SC.C 读取，可按章节覆盖。
PAPER_PALETTE = {"paper": "#F6EFDD", "card": "#FFFBF1", "ink": "#2E2B3A", "mute": "#8C8497",
                 "line": "#D9C9A6", "q": "#E8735A", "k": "#2C9F95", "v": "#6B62D6",
                 "gold": "#F0B23E", "blue": "#3A6FD8"}
FONT = '"Microsoft YaHei","PingFang SC","Noto Sans CJK SC","Noto Sans SC",sans-serif'


@lru_cache(maxsize=1)
def chapter_runtime() -> str:
    return Path(__file__).with_name("chapter_runtime.js").read_text(encoding="utf-8")


def _script_safe(code: str) -> str:
    # 原始脚本里出现 </script 会提前结束标签。
    return code.replace("</script", "<\\/script")


def compile_chapter(director_js: str | list[tuple[str, str]], timing: dict, *, width: int = 1920, height: int = 1080,
                    series: str = "", title: str = "", highlights: dict[str, str] | None = None,
                    palette: dict[str, str] | None = None, presenter: bool = True,
                    data: dict | None = None) -> str:
    """生成整章 HTML。``presenter`` 为真时字幕右侧让出右下角讲解员。"""
    if not timing.get("lines") or timing.get("dur", 0) <= 0:
        raise ValueError("章节时间轴为空")
    colors = {**PAPER_PALETTE, **(palette or {})}
    payload = {"timing": timing, "width": width, "height": height, "palette": colors,
               "highlights": highlights or {}, "font": FONT, "vars": data or {}}
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c").replace("&", "\\u0026")
    sub_right = int(width * .27) if presenter else 120
    c = colors
    css = f"""
html,body{{margin:0;width:{width}px;height:{height}px;overflow:hidden;background:{c['paper']}}}
body{{font-family:{FONT};color:{c['ink']}}}
#stage{{position:absolute;left:0;top:0;width:{width}px;height:{height}px;overflow:hidden;
 background:radial-gradient(ellipse at 50% 40%,rgba(255,252,243,.95) 0%,rgba(246,239,221,0) 70%),
 radial-gradient(circle,rgba(140,120,80,.13) 1.4px,transparent 1.6px) 0 0/36px 36px,{c['paper']}}}
#stage::after{{content:"";position:absolute;inset:0;pointer-events:none;
 background:radial-gradient(ellipse at 50% 50%,transparent 60%,rgba(160,130,80,.12) 100%)}}
.scene{{position:absolute;inset:0;display:none;will-change:opacity,transform,filter}}
.scene svg{{position:absolute;inset:0;width:{width}px;height:{height}px;overflow:visible}}
svg text{{font-family:{FONT}}}
.math{{font-family:"Cambria Math","Cambria","Times New Roman",serif;font-style:italic}}
.hand{{font-family:"KaiTi","STKaiti","Kaiti SC","Noto Serif CJK SC",serif}}
#tag{{position:absolute;left:56px;top:40px;display:flex;align-items:center;gap:14px;font-size:22px;color:{c['mute']};letter-spacing:1px;z-index:5}}
#tag .dot{{width:12px;height:12px;border-radius:50%;background:{c['blue']};box-shadow:0 0 0 5px rgba(58,111,216,.15)}}
#tag b{{color:{c['ink']};font-weight:700}}
#subs{{position:absolute;left:120px;right:{sub_right}px;bottom:54px;height:120px;z-index:6;pointer-events:none}}
#sub{{position:absolute;left:0;right:0;bottom:0;text-align:center;font-size:40px;line-height:1.4;font-weight:700;
 color:{c['ink']};letter-spacing:1px;opacity:0;text-wrap:balance;
 text-shadow:0 0 6px {c['paper']},0 0 12px {c['paper']},0 0 18px {c['paper']},0 2px 0 {c['paper']}}}
#sub em{{font-style:normal}}
#prog{{position:absolute;left:0;bottom:0;height:5px;background:linear-gradient(90deg,{c['blue']},#7FA6F0);z-index:8;opacity:.7}}
"""
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;")  # noqa: E731
    tag = (f'<div id="tag"><span class="dot"></span><span>{esc(series)}</span><b>{esc(title)}</b></div>'
           if series or title else "")
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            f"<style>{css}</style></head><body><div id=\"stage\">{tag}<div id=\"scenes\"></div>"
            "<div id=\"subs\"><div id=\"sub\"></div></div><div id=\"prog\"></div></div>"
            f'<script id="scid-chapter-v1" type="application/json">{data}</script>'
            f"<script>{_script_safe(chapter_runtime())}</script>"
            + _director_scripts(director_js) +
            "<script>window.__chapterStart();</script></body></html>")


def _director_scripts(director_js: str | list[tuple[str, str]]) -> str:
    """每个场景单独一个 script：某个场景的语法错误不会让整章停摆，并能标出是哪个场景。"""
    parts = [("", director_js)] if isinstance(director_js, str) else director_js
    head = "const {" + ", ".join(_EXPORTS) + "}=SC;"
    out = ["<script>window.addEventListener('error',e=>{window.__errors.push("
           "(window.__sceneTag?`[场景 ${window.__sceneTag}] `:'')+'脚本错误：'+e.message);});</script>"]
    for scene_id, code in parts:
        out.append(f"<script>window.__sceneTag={json.dumps(scene_id)};</script>"
                   f"<script>(()=>{{{head}\n{_script_safe(code)}\n}})();</script>")
    out.append("<script>window.__sceneTag='';</script>")
    return "".join(out)


#: 导演代码可直接使用的名字（与 chapter_runtime.js 的 window.SC 一致）。
_EXPORTS = ("DATA", "V", "DUR", "W", "H", "C", "T", "TE", "K", "KE", "clamp", "lerp", "Ez", "P", "Wn", "mix",
            "softmax", "pct", "rng", "S", "A", "O", "TR", "TRS", "txt", "drawPath", "DP", "textW", "chip",
            "rowXs", "panel", "heading", "arcD", "arcPt", "flowDots", "vecRow", "matrix", "bars", "pop", "scene")


async def _preflight_async(html: Path, times: list[float], width: int, height: int, executable: str,
                           layout_times: list[float] = ()) -> list[str]:
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=executable or None,
                                           args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
        try:
            page = await browser.new_page(viewport={"width": width, "height": height})
            problems: list[str] = []
            page.on("pageerror", lambda exc: problems.append(f"页面异常：{exc}"))
            await page.goto(html.resolve().as_uri(), wait_until="load")
            await page.wait_for_function("() => window.__ready === true", timeout=30_000)
            for t in times:
                await page.evaluate("(t) => window.__seek(t)", t)
            tagged = await page.evaluate("() => window.__errors.slice()")
            # 页面级异常若已被带场景标记的错误覆盖，只保留后者（便于定点修复）。
            problems = [p for p in problems if not any(p.removeprefix("页面异常：") in t for t in tagged)] + tagged
            # 版式：在每句话的稳定时刻检查文字重叠、出画与占用字幕/讲解员区。
            for t in layout_times:
                for issue in await page.evaluate("(t) => (window.__seek(t), window.__layout())", t):
                    issue = f"{issue}（t={t:.1f}s）"
                    key = re.sub(r"\(x[^)]*\)", "", issue.split("（t=")[0])
                    if not any(key == re.sub(r"\(x[^)]*\)", "", p.split("（t=")[0]) for p in problems):
                        problems.append(issue)
            # 倒序再 seek 一次并逐场景比对：画面必须只由 t 决定（隐藏场景不参与绘制）。
            snap = ("(t) => (window.__seek(t), Object.fromEntries([...document.querySelectorAll('#scenes > .scene')]"
                    ".filter(d => d.style.display !== 'none').map(d => [d.dataset.scene || '', d.style.cssText + d.innerHTML])))")
            probe = [times[len(times) // 4], times[len(times) // 2], times[3 * len(times) // 4]]
            first = [await page.evaluate(snap, t) for t in probe]
            for t in reversed(times):
                await page.evaluate("(t) => window.__seek(t)", t)
            again = [await page.evaluate(snap, t) for t in probe]
            for t, a, b in zip(probe, first, again):
                for scene_id in a:
                    if a[scene_id] != b.get(scene_id):
                        tag = f"[场景 {scene_id}] " if scene_id else ""
                        problems.append(f"{tag}t={t:.1f}s 两次 seek 画面不同：动画依赖调用顺序或保存了跨帧状态，"
                                        "必须只由 t 计算（不要在 update 里累加、不要用 Math.random）")
            return problems
        finally:
            await browser.close()


def preflight_chapter(html: Path, timing: dict, *, width: int = 1920, height: int = 1080,
                      executable: str = "") -> list[str]:
    """在多个时刻 seek，收集脚本错误与 seek 不确定性。空列表表示通过。"""
    dur = float(timing["dur"])
    times = sorted({round(dur * k / 40, 3) for k in range(41)} |
                   {round(line["start"] + .4, 3) for line in timing["lines"]})
    # 句末前 0.15 秒：入场动作已经完成、下一句还没开始。
    layout_times = [round(max(line["start"] + .6, line["end"] - .15), 3) for line in timing["lines"]]
    return asyncio.run(_preflight_async(Path(html), times, width, height, executable, layout_times))


def render_chapter(html: Path, timing: dict, out_dir: Path, *, audio: Path | None = None, fps: int = 30,
                   width: int = 1920, height: int = 1080, executable: str = "", workers: int = 0) -> Path:
    """逐帧截图 → 交付编码；有旁白时按片头留白对齐混入。返回成片路径。"""
    from ..html_capture import DEFAULT_WORKERS
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("找不到 ffmpeg")
    out_dir = Path(out_dir).resolve()
    frames = out_dir / "frames"
    if frames.exists():
        shutil.rmtree(frames)
    frames.mkdir(parents=True)
    total = math.ceil(float(timing["dur"]) * fps)
    rc = capture(html=Path(html), frames_dir=frames, total_frames=total, fps=fps, width=width, height=height,
                 start_sec=0.0, executable=executable, workers=workers or DEFAULT_WORKERS)
    if rc != 0:
        raise RuntimeError(f"章节逐帧截图失败（退出码 {rc}）")
    target = out_dir / "chapter.mp4"
    cmd = [ffmpeg, "-v", "error", "-y", "-framerate", str(fps), "-i", str(frames / "frame_%05d.jpg")]
    if audio:
        lead_ms = int(round(float(timing.get("lead", 0)) * 1000))
        cmd += ["-i", str(audio), "-filter_complex",
                f"[1:a]adelay={lead_ms}|{lead_ms},apad,aresample=48000[a]", "-map", "0:v", "-map", "[a]",
                "-c:a", "aac", "-b:a", "192k", "-ac", "2"]
    # 与 Go 侧 deliveryVideoArgs 一致的单次交付编码。
    cmd += ["-c:v", "libx264", "-preset", "slow", "-crf", "16", "-tune", "animation",
            "-maxrate", "12M", "-bufsize", "24M", "-pix_fmt", "yuv420p",
            "-t", f"{float(timing['dur']):.3f}", "-movflags", "+faststart", str(target)]
    proc = subprocess.run(cmd, capture_output=True, timeout=1800, check=False)
    if proc.returncode != 0:
        raise RuntimeError("章节编码失败：" + proc.stderr.decode("utf-8", "replace")[-500:])
    return target
