"""Offline browser checks with an explicitly synthetic job and intercepted APIs.

Run against a local web dev/preview server. No real jobs, queues or AI calls.
The fixture is any compiler-produced, single-shot fy HTML with objects.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import io
import json
import re
from pathlib import Path

from PIL import Image, ImageStat
from playwright.async_api import async_playwright, expect

from scidirector_ai.fy_objects import apply_fy_revision, extract_fy_snapshot, parse_fy_revision


async def verify(args: argparse.Namespace) -> None:
    code = args.fixture.read_text(encoding='utf-8')
    snapshot = extract_fy_snapshot(code)
    assert snapshot is not None
    payload = json.loads(re.search(r'<script id="scid-fy-data" type="application/json">(.*?)</script>', code, re.S)[1])
    objects = snapshot.document.objects
    assert len(objects) >= 2
    selected = objects[1]
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    shot = {'shot_id': 'editor-diagnostic', 'index': 1, 'tag': 'MOTION', 'engine': 'motion',
            'duration_sec': payload['timing']['dur'], 'narration': 'Isolated UI diagnostic, not a published video.',
            'visual_brief': 'Object editing diagnostic.', 'status': 'AWAITING_HUMAN', 'attempt': 1,
            'code': code, 'language': 'html', 'feedbacks': [], 'repair_tasks': []}
    detail = {'job': {'job_id': 'editor-diagnostic', 'raw_script': 'Isolated UI diagnostic.', 'locale': 'zh-CN',
                     'target_duration_sec': payload['timing']['dur'], 'status': 'PARTIAL', 'shots': [shot],
                     'style_guide': {'animation_style': 'fy', 'fy_style_id': 'X17'}, 'created_at': '2026-11-10T00:00:00Z'},
              'stat': {'total': 1, 'approved': 0, 'failed': 0, 'awaiting_human': 1, 'in_progress': 0}, 'progress': 0}
    posts: list[dict] = []
    server_detail = copy.deepcopy(detail)
    errors: list[str] = []

    async def route(request_route):
        request = request_route.request
        path = request.url.split('/api/v1/', 1)[1].split('?', 1)[0]
        if path.endswith('/reject'):
            posts.append(request.post_data_json)
            await request_route.fulfill(json={'ok': True, 'data': {'job_id': 'editor-diagnostic', 'shot_id': shot['shot_id'], 'status': 'RETRYING', 'attempt': 2}})
        elif path == 'jobs/editor-diagnostic':
            await request_route.fulfill(json={'ok': True, 'data': server_detail})
        elif path.startswith('styles/fy/'):
            await request_route.fulfill(json={'ok': True, 'data': {'id': 'X17', 'name': 'Isolated fixture', 'content': 'Diagnostic only', 'sha256': 'a' * 64, 'repository': '', 'attribution': ''}})
        elif path == 'styles/fy':
            await request_route.fulfill(json={'ok': True, 'data': {'entries': [{'id': 'X17', 'name': 'Isolated fixture', 'category': 'diagnostic'}], 'prompt_count': 1, 'license_note': 'UI diagnostic only'}})
        else:
            await request_route.fulfill(json={'ok': True, 'data': []})

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=args.chrome, headless=True)
        context = await browser.new_context(viewport={'width': 1440, 'height': 1080}, device_scale_factor=1)
        await context.route('**/api/v1/**', route)
        await context.add_init_script("""window.WebSocket = class {
          static OPEN = 1; readyState = 1;
          constructor() { window.__editorTestWS = this; setTimeout(() => {
            this.onopen?.({}); this.onmessage?.({data: JSON.stringify({type:'snapshot', data:window.__editorTestDetail})});
          }, 50); }
          send() {} close() { this.readyState = 3; }
        };""")
        page = await context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))

        async def load(data: dict):
            await page.add_init_script('window.__editorTestDetail = ' + json.dumps({**data, 'events': []}) + ';')
            await page.goto(args.url + '/?job=editor-diagnostic', wait_until='networkidle')
            await expect(page.locator('.ant-table-row')).to_have_count(1)
            if await page.get_by_role('button', name='展开行', exact=True).count():
                await page.get_by_role('button', name='展开行', exact=True).click()
            await expect(page.get_by_role('button', name='对象编辑', exact=True)).to_be_visible()

        async def geometry():
            frame = next(f for f in page.frames if f.url == 'about:srcdoc')
            return await frame.evaluate('window.__fyObjects()')

        async def wait_preview():
            await expect(page.locator('.fy-object-bound')).to_have_count(len(objects), timeout=15000)

        await load(detail)
        await page.get_by_role('button', name='对象编辑', exact=True).click()
        await wait_preview()
        await page.locator('.fy-item').filter(has_text=selected.id).click()
        await page.locator('.fy-editor-modal').screenshot(path=str(root / 'desktop-before.png'))
        x = page.get_by_role('spinbutton', name='横向位置', exact=True)
        x_before = selected.box.x * 100
        x_after = x_before + 3 if selected.box.x + selected.box.width + .03 < 1 else x_before - 3
        await x.fill(str(x_after)); await x.press('Tab')
        await expect(x).to_have_value(f'{x_after:.3f}')
        await wait_preview()
        assert abs(next(o for o in (await geometry())['objects'] if o['id'] == selected.id)['rect']['left'] / payload['width'] - x_after / 100) < .0001
        await page.get_by_role('button', name='撤销', exact=True).click()
        await expect(x).to_have_value(f'{x_before:.3f}')
        await page.get_by_role('button', name='重做', exact=True).click()
        await expect(x).to_have_value(f'{x_after:.3f}')
        await page.get_by_text('修改前', exact=True).first.click()
        await wait_preview()
        assert abs(next(o for o in (await geometry())['objects'] if o['id'] == selected.id)['rect']['left'] / payload['width'] - selected.box.x) < .0001
        await expect(page.get_by_role('button', name='提交修改并复审', exact=True)).to_be_disabled()
        await page.get_by_text('调整后', exact=True).first.click()
        await wait_preview()

        bound = page.get_by_role('button', name=f'选择对象 {selected.name or selected.id}', exact=True)
        bounds = await bound.bounding_box()
        await page.mouse.move(bounds['x'] + bounds['width'] / 2, bounds['y'] + bounds['height'] / 2)
        await page.mouse.down()
        await page.mouse.move(bounds['x'] + bounds['width'] / 2, bounds['y'] + bounds['height'] / 2 - 15, steps=5)
        await page.mouse.up()
        await wait_preview()
        assert float(await page.get_by_role('spinbutton', name='纵向位置', exact=True).input_value()) < selected.box.y * 100

        await x.fill(str(objects[0].box.x * 100)); await x.press('Tab'); await wait_preview()
        await expect(page.locator('.fy-issue-link').first).to_be_visible()
        await expect(page.get_by_role('button', name='提交修改并复审', exact=True)).to_be_disabled()
        await page.locator('.fy-issue-link').first.click()
        await page.get_by_role('button', name='撤销', exact=True).click(); await wait_preview()
        await expect(page.locator('.fy-issues')).to_have_count(0)

        await page.get_by_text(f'连线 {len(snapshot.document.relations)}', exact=True).click()
        if snapshot.document.relations:
            await page.locator('.ant-select[aria-label="起点端口"]').click()
            await page.locator('.ant-select-dropdown:not(.ant-select-dropdown-hidden)').get_by_text('上', exact=True).click()
            await wait_preview()
            assert (await geometry())['relations'][0]['source_port'] == 'top'
        await page.locator('.fy-editor-modal').screenshot(path=str(root / 'desktop-after.png'))

        frame = next(f for f in page.frames if f.url == 'about:srcdoc')
        pixels = await frame.evaluate('window.__fyPixels()')
        image = Image.open(io.BytesIO(base64.b64decode(pixels.split(',')[1]))).convert('RGB')
        assert max(ImageStat.Stat(image).stddev) > 10, 'blank canvas'
        assert await page.locator('iframe[title="fy 对象草稿画面"]').get_attribute('sandbox') == 'allow-scripts'
        await page.get_by_role('button', name='播放', exact=True).click()
        await page.wait_for_timeout(350)
        await page.get_by_role('button', name='暂停', exact=True).click()
        assert float((await page.locator('.fy-playback output').inner_text()).split(' / ')[0]) > .1
        await expect(page.get_by_role('button', name='提交修改并复审', exact=True)).to_be_enabled()
        await page.get_by_role('button', name='提交修改并复审', exact=True).click()
        await expect(page.locator('.fy-editor-modal')).to_have_count(0)
        assert len(posts) == 1
        revision = parse_fy_revision(posts[0]['comment'])
        assert revision is not None and revision.expect_revision == snapshot.revision
        changed_doc = apply_fy_revision(snapshot, revision)
        assert next(o for o in changed_doc.objects if o.id == selected.id).box.x == x_after / 100
        assert all(p.id == selected.id for p in revision.update_objects)
        assert 'code' not in posts[0] and 'narration' not in posts[0]

        # Fetch-time conflict must keep the local draft and never enqueue a patch.
        await page.get_by_role('button', name='对象编辑', exact=True).click(); await wait_preview()
        await page.get_by_role('spinbutton', name='层级', exact=True).fill('3')
        await page.get_by_role('spinbutton', name='层级', exact=True).press('Tab'); await wait_preview()
        server_detail['job']['shots'][0]['narration'] = 'Concurrent truthful edit.'
        await page.get_by_role('button', name='提交修改并复审', exact=True).click()
        await expect(page.get_by_text('镜头版本已更新，本地草稿不会覆盖新版本。', exact=True)).to_be_visible()
        assert len(posts) == 1
        await page.locator('.fy-editor-modal').screenshot(path=str(root / 'stale.png'))

        server_detail = copy.deepcopy(detail)
        await page.set_viewport_size({'width': 390, 'height': 844})
        await load(detail)
        await page.get_by_role('button', name='对象编辑', exact=True).click(); await wait_preview()
        await page.locator('.fy-editor-modal').screenshot(path=str(root / 'mobile.png'))
        modal_bounds = await page.locator('.fy-editor-modal').bounding_box()
        assert modal_bounds['x'] >= 0 and modal_bounds['x'] + modal_bounds['width'] <= 391
        assert modal_bounds['y'] + modal_bounds['height'] <= 845
        await expect(page.get_by_role('button', name='提交修改并复审', exact=True)).to_be_in_viewport()
        assert await page.locator('.fy-editor-properties').evaluate('(e) => e.scrollWidth <= e.clientWidth + 1')

        legacy = copy.deepcopy(detail); legacy['job']['shots'][0]['code'] = '<html>Legacy fy without objects</html>'
        server_detail = copy.deepcopy(legacy)
        await load(legacy)
        await expect(page.get_by_role('button', name='对象编辑', exact=True)).to_be_disabled()
        busy = copy.deepcopy(detail); busy['job']['shots'][0]['status'] = 'GENERATING'
        server_detail = copy.deepcopy(busy)
        await load(busy)
        await expect(page.get_by_role('button', name='对象编辑', exact=True)).to_be_disabled()
        assert not errors, errors
        await browser.close()

    (root / 'verification.json').write_text(json.dumps({'synthetic_job_only': True, 'real_api_calls': 0,
        'checks': ['actual canvas pixels', 'object geometry', 'numeric edit', 'undo/redo', 'before/after',
                   'pointer drag', 'ports', 'playback', 'layout warning blocks submit', 'warning selects target',
                   'versioned submission', 'Python patch validation', 'stale submission blocked', 'mobile fit with visible footer',
                   'legacy disabled', 'busy disabled'], 'posts': posts, 'page_errors': errors}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'fy editor browser checks passed: {root}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:5173')
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('.data/previews/fy-editor'))
    parser.add_argument('--chrome', default='C:/Program Files/Google/Chrome/Application/chrome.exe')
    asyncio.run(verify(parser.parse_args()))
