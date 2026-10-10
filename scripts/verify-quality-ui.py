"""Browser acceptance of creation/import/review, using isolated HTTP/WS fixtures.

Run scripts/render-anime-demo.py --render and extract preview.png into
.data/anime-demo first; start Vite on localhost:5173. Backend security and media
tests run separately.
Screenshots and the machine-readable result go to ignored .data/ui-verification.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".data/ui-verification"
OUT.mkdir(parents=True, exist_ok=True)
BASE = os.environ.get("SCID_UI_TEST_URL", "http://127.0.0.1:5173")
task = dict(task_id="r1",category="readability",severity="major",start_sec=1,end_sec=4,
            target="#angle-label",evidence="角度标注过小",instruction="将字号增大到 44px",
            acceptance="角度标注完整可读",status="resolved",resolution_evidence="当前证据帧 2 字号已增大")
feedback = dict(passed=False,score=.72,source="VLM",attempt=2,issues=["需复核角度标注"],
                suggestions=["请检查实际画面"],logic_score=.9,readability_score=.7,
                pacing_score=.7,aesthetics_score=.7,repair_tasks=[task],created_at="2026-10-10T10:00:00Z")
artifact = dict(artifact_id="a2",shot_id="s1",video_path="/server/shot.mp4",attempt=2,
                duration_sec=8,width=1280,height=720,fps=24,engine="d3",frame_samples=["frame1.png","frame2.png"])
shot = dict(shot_id="s1",index=1,tag="ILLUSTRATION",engine="d3",duration_sec=8,
            narration="反射角与入射角相等。角度始终从法线量起。",visual_brief="展示反射定律与角度标注。",
            status="AWAITING_HUMAN",attempt=2,artifact=artifact,
            feedbacks=[{**feedback,"attempt":1,"score":.58,"repair_tasks":[{**task,"status":"open"}]},feedback])
job = dict(job_id="ui-quality",status="RENDERING",raw_script=shot["narration"],target_duration_sec=8,
           shots=[shot],created_at="2026-10-10T10:00:00Z")
detail = dict(job=job,progress=.5,stat=dict(total=1,approved=0,awaiting_human=1,failed=0,in_progress=0))


def main():
    pending, generate_calls, errors = [], [], []
    state = {"upload":"hold", "generate":"fail", "media_fail":False}
    with sync_playwright() as pw:
        options = {"headless":True}
        if os.environ.get("SCID_CHROME"): options["executable_path"] = os.environ["SCID_CHROME"]
        browser = pw.chromium.launch(**options)
        page = browser.new_page(viewport={"width":1440,"height":1000},reduced_motion="reduce")
        page.on("pageerror", lambda error: errors.append(str(error)))

        def api(route):
            path = route.request.url.split("?")[0]
            if path.endswith("/assets/live2d"):
                if state["upload"] == "hold": pending.append(route); return
                route.fulfill(status=400,json={"message":"模型 ZIP 缺少 moc3，请重新打包。"}); return
            if path.endswith("/generate"):
                generate_calls.append(route.request.post_data_json)
                if state["generate"] == "fail":
                    route.fulfill(status=503,json={"message":"测试依赖暂未就绪，请重试。"}); return
                route.fulfill(json={"ok":True,"data":{"job_id":"ui-quality","target_duration_sec":8}}); return
            if path.endswith("/estimate-duration"):
                route.fulfill(json={"ok":True,"data":{"duration_sec":8,"basis":"fixture"}}); return
            if "/frames/" in path:
                if state["media_fail"]: route.fulfill(status=404); return
                route.fulfill(path=str(ROOT/".data/anime-demo/preview.png"),content_type="image/png"); return
            if path.endswith("/artifact"):
                data=(ROOT/".data/anime-demo/anime-demo_motion.mp4").read_bytes()
                match=re.match(r"bytes=(\d+)-(\d*)",route.request.headers.get("range", ""))
                if match:
                    start=int(match[1]); end=min(int(match[2]) if match[2] else len(data)-1,len(data)-1)
                    route.fulfill(status=206,body=data[start:end+1],content_type="video/mp4",
                        headers={"Accept-Ranges":"bytes","Content-Range":f"bytes {start}-{end}/{len(data)}"})
                else: route.fulfill(body=data,content_type="video/mp4",headers={"Accept-Ranges":"bytes"})
                return
            route.fulfill(json={"ok":True,"data":detail})

        page.route("**/api/**",api)
        page.route_web_socket("**/ws/jobs/*",lambda ws: ws.send(json.dumps({"type":"snapshot","data":detail})))
        page.goto(BASE)
        submit = page.get_by_role("button",name="开始生成",exact=True)
        submit.click()
        expect(page.get_by_text("请输入脚本",exact=True)).to_be_visible()
        script = page.locator("#raw_script")
        script.fill("光的反射遵循明确的几何规律：反射角等于入射角，二者都从法线量起。")
        animation = page.locator("#animation_style")
        animation.focus(); page.keyboard.press("ArrowDown")
        expect(page.locator(".ant-select-dropdown:not(.ant-select-dropdown-hidden)")).to_be_visible()
        page.keyboard.press("Escape")
        upload = page.locator('input[type=file][accept=".zip"]')
        upload.set_input_files({"name":"character.zip","mimeType":"application/zip","buffer":b"test"})
        expect(page.get_by_text("正在校验模型…",exact=True)).to_be_visible()
        expect(submit).to_be_disabled()
        assert len(pending) == 1
        pending.pop().fulfill(json={"ok":True,"data":{"asset_id":"a"*32,"filename":"character.zip","mouth_parameters":["ParamMouthOpenY"]}})
        expect(page.get_by_text("已导入：character.zip",exact=True)).to_be_visible()
        expect(submit).to_be_enabled()
        page.get_by_label("口型参数（留空读取模型 LipSync 分组）").fill("ParamMouthOpenY")
        state["upload"] = "fail"
        upload.set_input_files({"name":"broken.zip","mimeType":"application/zip","buffer":b"bad"})
        expect(page.get_by_text("模型 ZIP 缺少 moc3，请重新打包。",exact=True)).to_be_visible()
        expect(page.get_by_text("已导入：character.zip",exact=True)).to_be_visible()
        submit.click()
        expect(page.get_by_text("测试依赖暂未就绪，请重试。",exact=True)).to_be_visible()
        assert script.input_value().startswith("光的反射")
        assert generate_calls[-1]["effects"]["presenter"]["asset_id"] == "a"*32
        assert generate_calls[-1]["style_guide"]["animation_style"] == "anime"
        page.screenshot(path=str(OUT/"create-desktop.png"),full_page=True)
        page.set_viewport_size({"width":390,"height":844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        page.screenshot(path=str(OUT/"create-mobile.png"),full_page=True)
        state["generate"] = "success"
        try: submit.click(timeout=8000)
        except Exception:
            page.screenshot(path=str(OUT/"failure.png"),full_page=True)
            print(page.locator('body').inner_text(),flush=True)
            raise
        expect(page.get_by_text("镜头预览 · 第 2 版",exact=True)).to_be_visible()
        expect(page.get_by_text("已验证解决 1/1",exact=True)).to_be_visible()
        expect(page.get_by_text("较上次评分 +0.14",exact=True)).to_be_visible()
        assert "job=ui-quality" in page.url
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        page.screenshot(path=str(OUT/"review-mobile.png"),full_page=True)
        page.set_viewport_size({"width":1440,"height":1000})
        preview = page.get_by_label("第 2 版镜头视频")
        preview.scroll_into_view_if_needed()
        page.wait_for_function("document.querySelector('video').readyState >= 1")
        preview.evaluate("v => { v.currentTime = 2; }")
        page.wait_for_function("document.querySelector('video').currentTime >= 1.9")
        page.screenshot(path=str(OUT/"review-desktop.png"),full_page=True)
        page.get_by_role("button",name="打回重做",exact=True).click()
        expect(page.get_by_role("dialog")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.get_by_role("dialog")).not_to_be_visible()
        state["media_fail"] = True
        page.reload()
        expect(page.get_by_text("部分预览无法加载，文件可能已被清理或镜头已更新。",exact=True)).to_be_visible()
        expect(page.get_by_role("button",name="刷新任务",exact=True)).to_be_enabled()
        assert not errors, errors
        browser.close()
    result={"passed":True,"checks":["required validation","keyboard select and Escape","upload busy guard",
        "upload replacement failure retains model","submission failure retains form","successful creation and deep link",
        "current media and repair ledger","video seeking","390px reflow","modal keyboard","media failure recovery affordance",
        "reduced motion","no browser exceptions"],"api":"isolated fixtures; Go tests verify real HTTP behavior"}
    (OUT/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False))


if __name__ == "__main__": main()
