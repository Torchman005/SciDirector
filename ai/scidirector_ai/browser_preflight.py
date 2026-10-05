"""Sandbox entry point: inspect a handful of real frames without encoding a video."""
from __future__ import annotations

# Script-path execution must not let our logging.py shadow the standard library.
import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
sys.path[:] = [p for p in sys.path if str(Path(p or ".").resolve()) != _HERE]

import argparse  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
from typing import Any  # noqa: E402

# A small raster difference tolerates font/antialias noise but catches state accumulation.
DETERMINISM_CHANGE_RATIO = .005
READY_TIMEOUT_MS = 8_000
MAX_ISSUES = 12

# Measure actual DOM/SVG text, including clipping ancestors. Unknown Canvas text is reported as a gap.
MEASURE_JS = r"""({minFont}) => {
  const issues=[], warnings=[], texts=[];
  const selector = el => el.id ? '#'+el.id : el.tagName.toLowerCase()+
    (typeof el.className==='string' && el.className ? '.'+el.className.trim().split(/\s+/).join('.') : '');
  const visible = el => {
    let opacity=1;
    for(let p=el; p; p=p.parentElement){
      const s=getComputedStyle(p);
      if(s.display==='none'||s.visibility==='hidden') return false;
      opacity*=Number(s.opacity);
    }
    return opacity>.9;
  };
  for(const el of document.querySelectorAll('body *')){
    if(['SCRIPT','STYLE','NOSCRIPT'].includes(el.tagName.toUpperCase()) || !visible(el)) continue;
    const nodes=Array.from(el.childNodes).filter(n=>n.nodeType===Node.TEXT_NODE && n.textContent.trim());
    if(!nodes.length) continue;
    const rects=[];
    for(const n of nodes){const r=document.createRange();r.selectNodeContents(n);rects.push(...r.getClientRects());}
    const valid=rects.filter(r=>r.width>0 && r.height>0);
    if(!valid.length) continue;
    const text=nodes.map(n=>n.textContent.trim()).join(' ').slice(0,60), id=selector(el);
    const s=getComputedStyle(el);let font=Number.parseFloat(s.fontSize), measurable=true;
    if(el instanceof SVGElement && el.getScreenCTM){
      const m=el.getScreenCTM();if(m) font*=Math.min(Math.hypot(m.a,m.b),Math.hypot(m.c,m.d));
    }else{
      for(let p=el;p;p=p.parentElement) if(getComputedStyle(p).transform!=='none') measurable=false;
    }
    if(measurable && font<minFont*.9) issues.push(`${id} 文字「${text}」实际字号 ${font.toFixed(1)}px，低于容差下限 ${(minFont*.9).toFixed(1)}px；拆行或调整区域，不能继续压小文字`);
    let clipped=valid.some(r=>r.left < -2 || r.top < -2 || r.right>innerWidth+2 || r.bottom>innerHeight+2);
    for(let p=el;p;p=p.parentElement){
      const ps=getComputedStyle(p), box=p.getBoundingClientRect();
      if(['hidden','clip','scroll','auto'].includes(ps.overflowX))
        clipped ||= valid.some(r=>r.left<box.left-2||r.right>box.right+2);
      if(['hidden','clip','scroll','auto'].includes(ps.overflowY))
        clipped ||= valid.some(r=>r.top<box.top-2||r.bottom>box.bottom+2);
    }
    if(clipped) issues.push(`${id} 文字「${text}」超出视口或被容器裁切；增大文字区域、拆行或分阶段呈现`);
    texts.push({id,text,rects:valid.map(r=>({left:r.left,right:r.right,top:r.top,bottom:r.bottom}))});
  }
  // Overlap can be intentional (shadows/formulas); report it for generation, never auto reject.
  for(let i=0;i<texts.length && warnings.length<4;i++) for(let j=i+1;j<texts.length;j++){
    if(texts[i].rects.some(a=>texts[j].rects.some(b=>
      Math.min(a.right,b.right)-Math.max(a.left,b.left)>4 &&
      Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>4)))
      warnings.push(`${texts[i].id} 与 ${texts[j].id} 文字区域相交，请核对是否遮挡`);
  }
  if(document.querySelector('canvas')) warnings.push('Canvas 文字/图形布局未进行 DOM 测量，仍需视觉审核');
  return {issues:issues.slice(0,12),warnings:warnings.slice(0,6)};
}"""


def _different(before: bytes, after: bytes) -> bool:
    from PIL import Image, ImageChops

    with Image.open(io.BytesIO(before)) as a, Image.open(io.BytesIO(after)) as b:
        a.thumbnail((320, 180)); b.thumbnail((320, 180))
        if a.size != b.size:
            return True
        diff = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
        changed = sum(1 for pixel in diff.getdata() if max(pixel) > 20)
        return changed / max(1, a.width * a.height) > DETERMINISM_CHANGE_RATIO


def inspect(html: Path, width: int, height: int, duration: float, min_font: int,
            executable: str = "") -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    issues: list[str] = []
    warnings: list[str] = []
    launch: dict[str, Any] = {"args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]}
    if executable:
        launch["executable_path"] = executable
    times = [min(.2, duration * .1), duration * .25, duration * .5, duration * .75,
             max(0, duration - .05)]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(timeout=READY_TIMEOUT_MS, **launch)
        try:
            page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
            page.on("pageerror", lambda error: issues.append(f"JavaScript 运行错误：{str(error)[:240]}"))
            # Same offline assumption as production; a preflight must not fetch assets from the network.
            page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(("file:", "data:", "blob:")) else route.abort())
            page.goto(html.resolve().as_uri(), wait_until="load", timeout=READY_TIMEOUT_MS)
            if not page.evaluate("() => typeof window.__seek === 'function'"):
                issues.append("window.__seek 运行时不存在；必须定义可调用的纯函数，不能只在注释里出现")
                return {"issues": issues[:MAX_ISSUES], "warnings": warnings}
            try:
                page.wait_for_function("() => window.__ready !== false", timeout=READY_TIMEOUT_MS)
                page.evaluate("() => document.fonts.ready")
                def seek(t: float) -> None:
                    page.evaluate("t => window.__seek(t)", t)
                    page.evaluate("() => new Promise(r => requestAnimationFrame(() => r()))")
                before: bytes | None = None
                for t in times:
                    seek(t)
                    measured = page.evaluate(MEASURE_JS, {"minFont": min_font})
                    # Entry transitions may move opaque text in from outside the viewport.
                    destination = warnings if t == times[0] else issues
                    destination.extend(f"{t:.2f}s：{issue}" for issue in measured["issues"])
                    warnings.extend(f"{t:.2f}s：{warning}" for warning in measured["warnings"])
                    if t == times[2]:
                        before = page.screenshot(type="png")
                seek(times[2])  # Backwards seek, then repeat: parallel capture needs both properties.
                after = page.screenshot(type="png")
                seek(times[2])
                repeated = page.screenshot(type="png")
                if before and (_different(before, after) or _different(after, repeated)):
                    issues.append(f"{times[2]:.2f}s 重复/倒序 seek 的画面不一致；每次完整设置对象状态，禁止累加位移、追加节点或依赖实时定时器")
            except Exception as exc:
                issues.append(f"关键时刻执行失败：{type(exc).__name__}: {str(exc)[:300]}")
        finally:
            browser.close()
    return {"issues": list(dict.fromkeys(issues))[:MAX_ISSUES],
            "warnings": list(dict.fromkeys(warnings))[:6]}


def main() -> int:
    parser = argparse.ArgumentParser(description="生成代码的关键帧预检")
    parser.add_argument("--html", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--min-font", type=int, required=True)
    parser.add_argument("--executable", default="")
    args = parser.parse_args()
    try:
        report = inspect(Path(args.html), args.width, args.height, args.duration, args.min_font, args.executable)
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        return 0
    except Exception as exc:
        print(f"预检不可用：{type(exc).__name__}: {str(exc)[:300]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
