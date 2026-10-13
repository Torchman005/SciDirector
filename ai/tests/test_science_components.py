import importlib.util
import os
from pathlib import Path

import pytest

from scidirector_ai.scene import SceneSpec, compile_scene
from scidirector_ai.schemas import StyleGuide


def test_axis_aligned_lines_keep_their_path_but_points_are_rejected():
    line=dict(id='link',kind='line',box=dict(x=.2,y=.3,width=.4,height=0),arrow=True)
    el=SceneSpec(elements=[line]).elements[0]
    assert el.kind=='polyline' and el.points[0].y==el.points[1].y==.5
    assert abs(el.box.y+el.box.height*.5-.3)<1e-9
    with pytest.raises(ValueError,match='非零长度'):
        SceneSpec(elements=[dict(line,box=dict(x=.2,y=.3,width=0,height=0))])


def test_grid_rejects_fake_content_and_unreadable_density():
    base=dict(id='tokens',kind='grid',box=dict(x=.1,y=.1,width=.2,height=.2))
    with pytest.raises(ValueError,match='cells'):
        SceneSpec(elements=[base])
    dense=SceneSpec(elements=[dict(**base,cells=[dict(label='Q')]*64,columns=8)])
    with pytest.raises(ValueError,match='网格'):
        compile_scene(dense,width=1280,height=720,duration=8,style=StyleGuide())


def test_icon_cannot_hide_a_card_label_even_if_the_label_is_above_it(tmp_path):
    from scidirector_ai.browser_preflight import inspect
    chrome=os.environ.get('SCID_CHROME')
    if not chrome: pytest.skip('Set SCID_CHROME for real browser acceptance')
    scene=SceneSpec(elements=[
        dict(id='panel',kind='card',text='SwiGLU 门控',font_size=40,layer=5,
             box=dict(x=.1,y=.1,width=.4,height=.3)),
        dict(id='gate',kind='icon',glyph='gate',color='secondary',layer=1,
             box=dict(x=.14,y=.17,width=.15,height=.16))])
    html=tmp_path/'overlap.html'
    html.write_text(compile_scene(scene,width=1280,height=720,duration=4,style=StyleGuide()),encoding='utf-8')
    assert any('遮挡' in issue for issue in inspect(html,1280,720,4,32,chrome)['issues'])


def test_science_components_have_repeatable_pixels_and_pass_preflight(tmp_path):
    from scidirector_ai.browser_preflight import inspect
    script=Path(__file__).parents[2]/'scripts/render-process-demo.py'
    spec=importlib.util.spec_from_file_location('demo',script);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    code=compile_scene(module.demo_scene(),width=1600,height=900,duration=10,style=StyleGuide(min_font_size=28,
        animation_style='anime',presenter_safe_area=dict(x=.78,y=.55,width=.22,height=.45)))
    html=tmp_path/'scene.html';html.write_text(code,encoding='utf-8')
    chrome=os.environ.get('SCID_CHROME')
    if not chrome: pytest.skip('Set SCID_CHROME for real browser acceptance')
    result=inspect(html,1600,900,10,28,chrome)
    assert result['issues']==[],result


@pytest.mark.parametrize('glyph', ['phone','calendar','book','film','scissors','microscope','parcel'])
def test_story_prop_vocabulary_is_validated(glyph):
    scene=SceneSpec(elements=[dict(id='prop',kind='icon',glyph=glyph,
        box=dict(x=.2,y=.2,width=.3,height=.4))])
    assert scene.elements[0].glyph==glyph
    if glyph!='scissors':
        with pytest.raises(ValueError,match='reveal'):
            SceneSpec(elements=[dict(id='prop',kind='icon',glyph=glyph,
                box=dict(x=.2,y=.2,width=.3,height=.4),keyframes=[dict(time=0,reveal=0)])])


def test_prop_story_stage_and_scissors_are_seek_deterministic(tmp_path):
    from playwright.sync_api import sync_playwright
    from scidirector_ai.browser_preflight import inspect

    chrome=os.environ.get('SCID_CHROME')
    if not chrome: pytest.skip('Set SCID_CHROME for real browser acceptance')
    script=Path(__file__).parents[2]/'scripts/render-prop-story-demo.py'
    spec=importlib.util.spec_from_file_location('prop_demo',script)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    style=StyleGuide(preset='paper',background_style='paper_stage',animation_style='anime',
        presenter_safe_area=dict(x=.78,y=.55,width=.22,height=.45))
    html=tmp_path/'prop.html'
    html.write_text(compile_scene(module.demo_scene(),width=1600,height=900,duration=10,
                                  style=style),encoding='utf-8')
    report=inspect(html,1600,900,10,32,chrome,style.presenter_safe_area.model_dump())
    assert report['issues']==[],report
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=chrome)
        try:
            page=browser.new_page(viewport=dict(width=1600,height=900))
            page.goto(html.as_uri())
            pose=lambda t: page.evaluate("""t=>{window.__seek(t);
              return document.querySelector('#cut svg g > g').getAttribute('transform')}""",t)
            opened=pose(2)
            closed=pose(9)
            assert opened!=closed
            assert pose(2)==opened
            assert pose(9)==closed
        finally:
            browser.close()
