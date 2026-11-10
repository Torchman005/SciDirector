"""Isolated browser acceptance with synthetic assets and fully intercepted APIs."""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image
from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


async def verify(url: str, chrome: str, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    encoded = io.BytesIO()
    Image.new('RGB', (64, 64), '#ef384a').save(encoded, format='PNG')
    image = encoded.getvalue()
    catalog = {'entries': [
        {'id': 'X17', 'name': '工程蓝图', 'category': '测试', 'fit': '技术原理'},
        {'id': 'L9', 'name': '漫画剧场', 'category': '测试', 'fit': '幽默故事'}],
        'prompt_count': 2, 'library_version': 'isolated-fixture', 'license_note': '离线界面测试数据'}
    asset = {'asset_id': 'a' * 32, 'filename': '离线测试.png', 'mime_type': 'image/png', 'role': 'character',
             'width': 64, 'height': 64, 'size_bytes': len(image),
             'generated': {'provider': 'relay', 'model': 'fixture-image', 'fy_style_id': 'X17'}}
    posts, errors = [], []
    slow_started, release_slow = asyncio.Event(), asyncio.Event()
    image_started, release_image = asyncio.Event(), asyncio.Event()
    async def route(r):
        path = r.request.url.split('/api/v1/', 1)[1].split('?', 1)[0]
        data = []
        if path == 'styles/fy/recommend':
            script = r.request.post_data_json['raw_script']
            if script.startswith('慢速'):
                slow_started.set()
                await release_slow.wait()
            style_id = 'L9' if script.startswith('幽默') else 'X17'
            data = {'style_id': style_id, 'reason': '匹配脚本用词：技术原理',
                    'library_version': 'isolated-fixture', 'method': 'catalog_keyword_match', 'alternatives': []}
        elif path == 'styles/fy':
            data = catalog
        elif path.startswith('styles/fy/'):
            style_id = path.rsplit('/', 1)[1]
            data = {'id': style_id, 'name': next(e['name'] for e in catalog['entries'] if e['id'] == style_id),
                    'content': '离线测试画风原文', 'sha256': 'b' * 64, 'library_version': 'isolated-fixture',
                    'repository': '', 'attribution': '离线测试'}
        elif path == 'image-providers':
            data = {'providers': [{'id': 'relay', 'label': '离线测试服务', 'model': 'fixture-image',
                                  'protocol': 'openai_images', 'sizes': ['1024x1024'], 'configured': True}]}
        elif path == 'assets/visuals/generate':
            posts.append(r.request.post_data_json)
            image_started.set()
            await release_image.wait()
            data = asset
        elif path == 'assets/visuals/' + asset['asset_id']:
            return await r.fulfill(content_type='image/png', body=image)
        elif path == 'estimate-duration':
            data = {'duration_sec': 20, 'basis': '离线测试'}
        try:
            await r.fulfill(json={'ok': True, 'data': data})
        except Exception:
            if not r.request.is_navigation_request():
                return  # A superseded recommendation is intentionally aborted.
            raise

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=chrome, headless=True)
        try:
            context = await browser.new_context(viewport={'width': 1440, 'height': 1080})
            await context.route('**/api/v1/**', route)
            page = await context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto(url, wait_until='networkidle')
            submit = page.get_by_role('button', name='开始生成', exact=True)
            await expect(submit).to_be_disabled()
            script = page.locator('#raw_script')
            await script.fill('通过一台蒸汽机的机械结构拆解，逐步讲解它的工作原理。')
            await expect(submit).to_be_enabled(timeout=10000)
            await expect(page.get_by_text('匹配脚本用词：技术原理', exact=True)).to_be_visible()
            await page.screenshot(path=str(output / 'desktop-style.png'))

            await script.fill('慢速请求：通过机械结构的拆解来理解蒸汽机的工作原理。')
            await slow_started.wait()
            await page.locator('.ant-select-selection-item').filter(has_text='X17 · 工程蓝图').click()
            await page.get_by_text('L9 · 漫画剧场', exact=True).click()
            release_slow.set()
            await expect(page.get_by_role('switch', name='按脚本自动匹配画风')).not_to_be_checked()
            await expect(page.locator('.ant-select-selection-item').filter(has_text='L9 · 漫画剧场')).to_be_visible()
            await expect(submit).to_be_enabled()
            await page.wait_for_timeout(1000)
            await expect(page.locator('.ant-select-selection-item').filter(has_text='L9 · 漫画剧场')).to_be_visible()

            await script.fill('幽默轻松的故事，从搞笑漫画的角度讲讲这台机器的趣事。')
            await page.get_by_role('switch', name='按脚本自动匹配画风').click()
            await expect(submit).to_be_enabled(timeout=10000)
            await expect(page.locator('.ant-select-selection-item').filter(has_text='L9 · 漫画剧场')).to_be_visible()

            await page.get_by_text('生成配图', exact=True).click()
            await page.get_by_role('textbox', name='配图描述', exact=True).fill('一台展开外壳的蒸汽机，侧面视角，无文字。')
            generate = page.get_by_role('button', name='生成配图', exact=True)
            await expect(generate).to_be_enabled()
            assert not posts
            await generate.click()
            await page.get_by_role('button', name='取消生成配图', exact=True).click()
            assert not posts
            await generate.click()
            await page.get_by_role('button', name='确认生成 1 张', exact=True).click()
            await image_started.wait()
            await expect(submit).to_be_disabled()
            release_image.set()
            await expect(page.get_by_role('checkbox', name='离线测试.png')).to_be_visible()
            await expect(page.get_by_role('checkbox', name='离线测试.png')).not_to_be_checked()
            await page.get_by_role('checkbox', name='离线测试.png').check()
            await expect(submit).to_be_enabled()
            assert len(posts) == 1 and posts[0]['confirm_cost'] is True and posts[0]['fy_style_id'] == 'L9'
            thumbnail = page.get_by_role('img', name=asset['filename'], exact=True)
            await thumbnail.scroll_into_view_if_needed()
            await expect(thumbnail).to_be_visible()
            await expect(thumbnail).to_have_js_property('complete', True)
            await expect(thumbnail).to_have_js_property('naturalWidth', 64)
            await generate.scroll_into_view_if_needed()
            await page.screenshot(path=str(output / 'desktop-images.png'))
            await page.set_viewport_size({'width': 390, 'height': 844})
            await generate.scroll_into_view_if_needed()
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await page.screenshot(path=str(output / 'mobile-images.png'))
            assert not errors, errors
            await context.close()
        finally:
            await browser.close()
    (output / 'verification.json').write_text(json.dumps({'synthetic': True,
        'paid_calls': 0, 'generation_confirmations': len(posts), 'manual_override_preserved': True,
        'mobile_no_overflow': True, 'generated_thumbnail_decoded': True,
        'page_errors': errors}, ensure_ascii=False, indent=2), encoding='utf-8')


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--chrome', default=r'C:\Program Files\Google\Chrome\Application\chrome.exe')
    parser.add_argument('--output', type=Path, default=ROOT / '.data/previews/visual-preparation')
    args = parser.parse_args()
    handler = partial(QuietHandler, directory=str(ROOT / 'web/dist'))
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        asyncio.run(verify(f'http://127.0.0.1:{server.server_port}', args.chrome, args.output))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
