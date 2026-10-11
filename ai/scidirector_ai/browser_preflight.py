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
MAX_SAMPLE_TIMES = 24

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
    const s=getComputedStyle(el);let font=Number.parseFloat(s.fontSize);
    if(el instanceof SVGElement && el.getScreenCTM){
      const m=el.getScreenCTM();if(m) font*=Math.min(Math.hypot(m.a,m.b),Math.hypot(m.c,m.d));
    }else{
      for(let p=el;p;p=p.parentElement){
        const transform=getComputedStyle(p).transform;
        if(transform!=='none'){
          const m=new DOMMatrixReadOnly(transform);
          font*=Math.min(Math.hypot(m.a,m.b),Math.hypot(m.c,m.d));
        }
      }
    }
    if(font<minFont*.9) issues.push(`${id} 文字「${text}」实际字号 ${font.toFixed(1)}px，低于容差下限 ${(minFont*.9).toFixed(1)}px；拆行或调整区域，不能继续压小文字`);
    let clipped=valid.some(r=>r.left < -2 || r.top < -2 || r.right>innerWidth+2 || r.bottom>innerHeight+2);
    for(let p=el;p;p=p.parentElement){
      const ps=getComputedStyle(p), box=p.getBoundingClientRect();
      if(['hidden','clip','scroll','auto'].includes(ps.overflowX))
        clipped ||= valid.some(r=>r.left<box.left-2||r.right>box.right+2);
      if(['hidden','clip','scroll','auto'].includes(ps.overflowY))
        clipped ||= valid.some(r=>r.top<box.top-2||r.bottom>box.bottom+2);
    }
    if(clipped) issues.push(`${id} 文字「${text}」超出视口或被容器裁切；增大文字区域、拆行或分阶段呈现`);
    const owner=el.closest('.scene-element');
    texts.push({id:owner?'#'+owner.id:id,text,owner,rects:valid.map(r=>({left:r.left,right:r.right,top:r.top,bottom:r.bottom}))});
  }
  const area=r=>Math.max(0,r.right-r.left)*Math.max(0,r.bottom-r.top);
  const overlap=(a,b)=>Math.max(0,Math.min(a.right,b.right)-Math.max(a.left,b.left))*
    Math.max(0,Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top));
  for(let i=0;i<texts.length;i++) for(let j=i+1;j<texts.length;j++){
    const a=texts[i],b=texts[j];
    if(a.owner && a.owner===b.owner) continue;
    if(a.rects.some(x=>b.rects.some(y=>overlap(x,y)>Math.min(area(x),area(y))*.12))){
      const output=a.owner&&b.owner?issues:warnings;
      output.push(`${a.id} 与 ${b.id} 文字重叠；分离文字区域或错开可见时间`);
    }
  }
  const shapes=Array.from(document.querySelectorAll('[data-scene-kind]'))
    .filter(e=>['rect','circle','card','character'].includes(e.dataset.sceneKind)&&visible(e));
  const sameGroup=(a,b)=>a.dataset.overlapGroup && a.dataset.overlapGroup===b.dataset.overlapGroup;
  const fill=e=>e.dataset.sceneKind==='card'?getComputedStyle(e).backgroundColor:
    e.querySelector('rect,circle')?.getAttribute('fill');
  for(let i=0;i<shapes.length;i++) for(let j=i+1;j<shapes.length;j++){
    const a=shapes[i],b=shapes[j],ar=a.getBoundingClientRect(),br=b.getBoundingClientRect();
    if(sameGroup(a,b)||['character'].includes(a.dataset.sceneKind)||b.dataset.sceneKind==='character') continue;
    const ac=fill(a),bc=fill(b);
    if(ac && ac!=='none' && ac===bc && overlap(ar,br)>Math.min(area(ar),area(br))*.12)
      issues.push(`#${a.id} 与 #${b.id} 同色图形重叠，边界无法区分；分离布局、增加不同填色/轮廓，确需叠合的复合图形标记相同 overlap_group`);
  }
  for(const text of texts) if(text.owner) for(const shape of shapes){
    if(text.owner===shape) continue;
    const a=Number(getComputedStyle(text.owner).zIndex)||0,b=Number(getComputedStyle(shape).zIndex)||0;
    const above=b>a || (b===a && !!(text.owner.compareDocumentPosition(shape)&Node.DOCUMENT_POSITION_FOLLOWING));
    if(!above) continue;
    const box=shape.getBoundingClientRect();
    if(text.rects.some(r=>overlap(r,box)>area(r)*.25))
      issues.push(`${text.id} 文字被前景 #${shape.id} 遮挡；把文字放到图形上层或移动角色/图形`);
  }
  if(document.querySelector('canvas')) warnings.push('Canvas 文字/图形布局未进行 DOM 测量，仍需视觉审核');
  return {issues:issues.slice(0,12),warnings:warnings.slice(0,6)};
}"""


def sample_times(duration: float, payload: dict[str, Any] | None = None) -> list[float]:
    """Probe stage boundaries and their midpoints, where crossings are easy to miss."""
    times = {min(.2, duration * .1), *(duration * i / 8 for i in range(1, 8)), max(0, duration - .05)}
    if payload:
        elements = payload.get("scene", {}).get("elements", [])
        keys = {k["time"] for el in elements for k in el.get("keyframes", [])}
        end = payload.get("animatedUntil", 1)
        scale = end if any(t > end for t in keys) else 1
        for el in elements:
            frames = el.get("keyframes", [])
            for k in frames:
                times.add(min(duration - .01, max(0, k["time"] * scale * duration)))
            for a, b in zip(frames, frames[1:]):
                times.add((a["time"] + b["time"]) / 2 * scale * duration)
    ordered = sorted(times)
    if len(ordered) > MAX_SAMPLE_TIMES:
        ordered = [ordered[round(i * (len(ordered) - 1) / (MAX_SAMPLE_TIMES - 1))] for i in range(MAX_SAMPLE_TIMES)]
    return ordered


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
                payload = page.evaluate("() => {const e=document.getElementById('scid-scene-v1');return e?JSON.parse(e.textContent):null}")
                times = sample_times(duration, payload)
                check_time = times[len(times) // 2]
                def seek(t: float) -> None:
                    page.evaluate("t => window.__seek(t)", t)
                    page.evaluate("() => new Promise(r => requestAnimationFrame(() => r()))")
                before: bytes | None = None
                for t in times:
                    seek(t)
                    measured = page.evaluate(MEASURE_JS, {"minFont": min_font})
                    # Entry transitions may move opaque text in from outside the viewport.
                    destination = warnings if t < min(.25, duration * .1) else issues
                    destination.extend(f"{t:.2f}s：{issue}" for issue in measured["issues"])
                    warnings.extend(f"{t:.2f}s：{warning}" for warning in measured["warnings"])
                    if t == check_time:
                        before = page.screenshot(type="png")
                seek(check_time)  # Backwards seek, then repeat: parallel capture needs both properties.
                after = page.screenshot(type="png")
                seek(check_time)
                repeated = page.screenshot(type="png")
                if before and (_different(before, after) or _different(after, repeated)):
                    issues.append(f"{check_time:.2f}s 重复/倒序 seek 的画面不一致；每次完整设置对象状态，禁止累加位移、追加节点或依赖实时定时器")
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
