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
