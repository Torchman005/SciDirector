import importlib.util
import os
from pathlib import Path

import pytest

from scidirector_ai.scene import SceneSpec, compile_scene
from scidirector_ai.schemas import StyleGuide


@pytest.mark.skipif(not os.environ.get('SCID_CHROME'),reason='需要 Chromium')
def test_layered_materials_are_readable_and_preserve_values(tmp_path):
    from scidirector_ai.browser_preflight import inspect
    from playwright.sync_api import sync_playwright
    source=Path(__file__).parents[2]/'scripts/render-attention-demo.py'
    spec=importlib.util.spec_from_file_location('attention_demo',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    scene=module.demo_scene()
    code=compile_scene(scene,width=1600,height=900,duration=10,style=StyleGuide(animation_style='anime'))
    file=tmp_path/'index.html';file.write_text(code,encoding='utf-8')
    assert not inspect(file,1600,900,10,32,os.environ['SCID_CHROME'])['issues']
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=os.environ['SCID_CHROME'])
        page=browser.new_page(viewport={'width':1600,'height':900})
        page.goto(file.as_uri());page.evaluate('window.__seek(8.5)')
        assert page.locator('#name_Q').evaluate('e=>getComputedStyle(e).zIndex')=='5'
        assert page.locator('#tile_Q linearGradient stop').count()==2
        assert page.locator('body').evaluate('e=>getComputedStyle(e).backgroundColor')!='rgba(0, 0, 0, 0)'
        before=page.screenshot()
        page.evaluate('window.__seek(0);window.__seek(10);window.__seek(8.5)')
        assert page.screenshot()==before
        browser.close()


def test_appearance_controls_reject_css_and_preserve_scientific_data():
    from pydantic import ValidationError
    e=dict(id='bars',kind='bars',box=dict(x=.1,y=.1,width=.8,height=.8),unit='ms',
           data=[dict(label='A',value=42,color='secondary'),dict(label='B',value=21,color='accent')])
    scene=SceneSpec(elements=[e])
    compile_scene(scene,width=1920,height=1080,duration=8,style=StyleGuide(animation_style='anime'))
    assert [d.value for d in scene.elements[0].data]==[42,21]
    for bad in [dict(stroke_color='url(https://x)'),dict(layer=999),dict(material='script')]:
        with pytest.raises(ValidationError):SceneSpec(elements=[{**e,**bad}])
