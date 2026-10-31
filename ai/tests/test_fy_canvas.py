"""Component provenance, chapter handoff and real Canvas/browser acceptance."""
from __future__ import annotations

import json
import hashlib
import os
import runpy
import sys
from pathlib import Path

import pytest

from scidirector_ai.chapter import fy
from scidirector_ai.chapter.fy_styles import load_fy_style
from scidirector_ai.chapter.narration import estimate_timing
from scidirector_ai.chapter.writer import ChapterScript, SceneCode
from test_chapter import _script_dict

CODE = "scene('hello', ({ctx,t,M,label,C}) => { label('当前对象', 800 + M.easeInOut(M.progress(t,0,2))*50, 400, {size:40}); });"


def script_and_timing():
    script = ChapterScript.model_validate(_script_dict())
    return script, estimate_timing(script.chapter_lines())


def test_pinned_components_are_present_and_style_library_is_not_implied() -> None:
    info = fy.engine_info()
    assert info['upstream']['commit'] == 'd9a545afa393b2ffab4f36e87371691daf8e502c'
    assert set(info['upstream']['files']) == {'motion-core.mjs', 'canvas-contract.mjs', 'LICENSE.md', 'style_library.py'}
    assert info['style_library'] is None and info['integration'] == 'canvas_trial'


def test_compile_embeds_actual_upstream_functions_and_canvas_adapter() -> None:
    _, timing = script_and_timing()
    html = fy.compile_fy([('hello', CODE)], timing, title='Canvas', data={'value': .65})
    assert 'function createCanvasContract' in html and 'function springAt' in html
    assert 'FYC.createCanvasContract' in html and 'canvas id="film"' in html
    assert 'window.__seek' in html and 'window.__fyPixels' in html
    assert 'src="http' not in html and html.count('<script') == html.count('</script>')


@pytest.mark.parametrize('codes', [[], [('missing', CODE)], [('hello', CODE), ('hello', CODE)]])
def test_compile_rejects_mismatched_or_duplicate_scene_manifest(codes) -> None:
    _, timing = script_and_timing()
    with pytest.raises(ValueError, match='场景'):
        fy.compile_fy(codes, timing)


class FakeLLM:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def chat_json(self, system, user, schema, **kwargs):
        self.calls.append((system, user))
        return schema.model_validate(self.replies.pop(0))


def test_scene_writer_uses_canvas_contract_and_includes_repair_evidence() -> None:
    script, timing = script_and_timing()
    llm = FakeLLM([{'code': CODE}])
    assert fy.write_fy_scene(llm, script, 0, timing, previous_code='old', problems=['文字重叠']) == CODE
    system, user = llm.calls[0]
    assert 'Canvas' in system and '未提供时使用项目纸面配色' in system
    assert '文字重叠' in user and 'old' in user


def test_workflow_repairs_before_render_and_writes_engine_provenance(tmp_path, monkeypatch) -> None:
    script, _ = script_and_timing()
    llm = FakeLLM([{'code': CODE}, {'code': CODE}])
    calls = []
    def preflight(*args, **kwargs):
        calls.append('check')
        return ['[场景 hello] 布局需要调整'] if len(calls) == 1 else []
    monkeypatch.setattr(fy, 'preflight_fy', preflight)
    monkeypatch.setattr(fy, 'render_chapter', lambda *args, **kwargs: calls.append('render') or tmp_path/'chapter.mp4')
    result = fy.make_fy_chapter(llm, '', tmp_path, script=script, tts=None)
    assert calls == ['check','check','render'] and len(llm.calls) == 2
    assert result.video.name == 'chapter.mp4'
    info = json.loads((tmp_path/'engine.json').read_text(encoding='utf-8'))
    assert info['engine'] == 'fy-canvas' and info['style_library'] is None
    assert '未使用 341' in result.warnings[0]


def test_workflow_never_renders_when_canvas_preflight_remains_bad(tmp_path, monkeypatch) -> None:
    script, _ = script_and_timing()
    llm = FakeLLM([{'code': CODE}] * (fy.MAX_REPAIRS + 1))
    monkeypatch.setattr(fy, 'preflight_fy', lambda *args, **kwargs: ['[场景 hello] 绘制失败'])
    monkeypatch.setattr(fy, 'render_chapter', lambda *args, **kwargs: pytest.fail('must not render'))
    with pytest.raises(RuntimeError, match='停止出片'):
        fy.make_fy_chapter(llm, '', tmp_path, script=script, tts=None)
    assert len(llm.calls) == fy.MAX_REPAIRS + 1 and (tmp_path/'preflight.json').exists()


@pytest.mark.skipif(not os.environ.get('SCID_CHROME'), reason='Set SCID_CHROME for real Canvas acceptance')
@pytest.mark.parametrize('code, expected', [
    (CODE, None),
    ("scene('hello', ({label}) => { label('甲',800,400);label('乙',800,400); });", '文字重叠'),
    ("scene('hello', ({label}) => { label('越界',1900,400); });", '出画'),
    ("scene('hello', ({K,label}) => { label(K('hello','不存在'),800,400); });", '不存在'),
    ("let n=0; scene('hello', ({label}) => { label(String(n++),800,400); });", '像素不同'),
    ("scene('hello', ({label}) => { label('小字',800,400,{size:18}); });", '字号过小'),
    ('const broken = ;', '未注册'),
])
def test_real_browser_checks_pixels_runtime_and_measured_text(tmp_path: Path, code: str, expected: str | None) -> None:
    _, timing = script_and_timing()
    path = tmp_path/'index.html'
    path.write_text(fy.compile_fy([('hello',code)],timing),encoding='utf-8')
    problems = fy.preflight_fy(path,timing,executable=os.environ['SCID_CHROME'])
    if expected is None:
        assert problems == []
    else:
        assert any(expected in p for p in problems), problems


@pytest.fixture
def selected_style(tmp_path):
    library = tmp_path/'library'
    prompts = library/'风格提示词'/'X_code-video'
    prompts.mkdir(parents=True)
    text = '# X17 蓝图\n\nBlueprint blue #17508c; white dimension lines.\n引用 `missing.js`。\n' + '完整原文，不是搜索摘要。\n'*80
    (prompts/'X17_蓝图.md').write_bytes(text.encode('utf-8'))
    (library/'LICENSE').write_text('fixture library license',encoding='utf-8')
    return load_fy_style('x17',library,output=tmp_path/'snapshot')


def test_selected_prompt_is_complete_versioned_and_snapshotted(selected_style,tmp_path):
    source=selected_style
    assert source['id']=='X17' and source['prompt_count']==1
    assert source['content'].endswith('完整原文，不是搜索摘要。\n'*80)
    assert source['sha256']==hashlib.sha256(source['content'].encode()).hexdigest()
    assert source['dependency_candidates'][0]['reference']=='missing.js'
    assert source['dependency_audit_complete'] is False
    assert 'candidate_path' not in source['dependency_candidates'][0]
    assert json.loads((tmp_path/'snapshot/style-source.json').read_text(encoding='utf-8'))==source
    assert (tmp_path/'snapshot/style-index.json').is_file()
    assert (tmp_path/'snapshot/style-licenses/LICENSE').read_text()=='fixture library license'
    assert fy.engine_info(source)['style_library']['sha256']==source['sha256']


def test_style_survives_planning_generation_and_repair(selected_style,tmp_path,monkeypatch):
    script,timing=script_and_timing()
    observed=[]
    def planner(*args,**kwargs):
        observed.append(kwargs['style'])
        return script
    monkeypatch.setattr(fy,'write_script',planner)
    results=iter([['[场景 hello] 文字重叠'],[]])
    monkeypatch.setattr(fy,'preflight_fy',lambda *args,**kwargs: next(results))
    llm=FakeLLM([{'code':CODE},{'code':CODE}])
    fy.make_fy_chapter(llm,'content',tmp_path/'chapter',tts=None,fy_style=selected_style,render=False)
    assert selected_style['content'] in json.loads(observed[0].split('：\n',1)[1])['content']
    for system,user in llm.calls:
        assert '完整原文，不是搜索摘要。' in user and 'X17' in user and 'theme' in system
    assert '文字重叠' in llm.calls[1][1]
    assert 'library_prompt' in (tmp_path/'chapter/engine.json').read_text(encoding='utf-8')


@pytest.mark.skipif(not os.environ.get('SCID_CHROME'), reason='Set SCID_CHROME for real Canvas acceptance')
@pytest.mark.parametrize('mode',['valid','missing','late'])
def test_real_theme_and_scene_boundaries(selected_style,tmp_path,mode):
    script,timing=script_and_timing()
    palette={**fy.PAPER_PALETTE,'paper':'#17508c','ink':'#f1f5f8','gold':'#ffd166'}
    init='theme('+json.dumps(palette)+');'
    a="scene('hello', ({label,C}) => label('甲',800,400,{color:C.ink}));"
    b="scene('sq', ({label}) => label('乙',800,400));"
    codes=[('hello',(init if mode=='valid' else '')+a),('sq',(init if mode=='late' else '')+b)]
    target=tmp_path/'theme.html'
    target.write_text(fy.compile_fy(codes,timing,style=selected_style),encoding='utf-8')
    problems=fy.preflight_fy(target,timing,executable=os.environ['SCID_CHROME'])
    if mode=='valid':
        assert problems==[]  # Identical positions in different scenes must not count as overlap.
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path=os.environ['SCID_CHROME'])
            page=browser.new_page()
            page.goto(target.as_uri())
            page.wait_for_function('window.__ready')
            assert page.evaluate("getComputedStyle(document.getElementById('sub')).color")=='rgb(241, 245, 248)'
            assert page.evaluate("getComputedStyle(document.body).backgroundColor")=='rgb(23, 80, 140)'
            assert page.evaluate('Object.isFrozen(FY.C)')
            browser.close()
    else:
        assert any('theme' in p for p in problems)


def test_style_selection_rejects_unknown_id_and_source_output(tmp_path):
    with pytest.raises(ValueError,match='编号'):
        load_fy_style('蓝图',tmp_path)
    root=tmp_path/'library'
    (root/'风格提示词').mkdir(parents=True)
    with pytest.raises(ValueError,match='画风库内部'):
        load_fy_style('X17',root,output=root/'output')
    with pytest.raises(ValueError,match='STYLE_NOT_FOUND'):
        load_fy_style('X999',root)


@pytest.mark.parametrize('engine',['svg','fy-canvas'])
def test_cli_routes_to_selected_backend_before_model_work(tmp_path,monkeypatch,engine):
    from types import SimpleNamespace
    from scidirector_ai import config, llm as llm_module
    from scidirector_ai.chapter import workflow, fy_styles
    script,timing=script_and_timing()
    calls=[]
    selected={'id':'X17'}
    monkeypatch.setattr(config,'get_settings',lambda: SimpleNamespace(chapter_code_provider='',tts_max_attempts=3))
    monkeypatch.setattr(llm_module,'LLMClient',lambda *args: object())
    def make(*args,**kwargs):
        calls.append((engine,kwargs))
        return fy.FYResult(script,timing,tmp_path/'index.html')
    def wrong(*args,**kwargs):
        pytest.fail('wrong engine')
    monkeypatch.setattr(fy,'make_fy_chapter',make if engine=='fy-canvas' else wrong)
    monkeypatch.setattr(workflow,'make_chapter',make if engine=='svg' else wrong)
    monkeypatch.setattr(fy_styles,'load_fy_style',lambda *args,**kwargs: selected)
    args=['make-chapter.py','--content','内容','--output',str(tmp_path/'out'),'--no-tts','--no-video','--engine',engine]
    if engine=='fy-canvas': args.extend(['--fy-style','X17'])
    monkeypatch.setattr(sys,'argv',args)
    runpy.run_path(str(Path(__file__).resolve().parents[2]/'scripts/make-chapter.py'),run_name='__main__')
    assert len(calls)==1
    if engine=='fy-canvas': assert calls[0][1]['fy_style'] is selected


def test_cli_rejects_style_on_svg_before_provider_creation(tmp_path,monkeypatch):
    from scidirector_ai import config
    monkeypatch.setattr(config,'get_settings',lambda: pytest.fail('should validate first'))
    monkeypatch.setattr(sys,'argv',['make-chapter.py','--content','内容','--output',str(tmp_path),'--fy-style','X17'])
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(Path(__file__).resolve().parents[2]/'scripts/make-chapter.py'),run_name='__main__')
    assert error.value.code==2


@pytest.mark.parametrize('engine',['svg','fy-canvas'])
def test_cli_rejects_removed_reference_option_before_model_work(tmp_path,monkeypatch,capsys,engine):
    from scidirector_ai import config
    monkeypatch.setattr(config,'get_settings',lambda: pytest.fail('should reject option before providers'))
    monkeypatch.setattr(sys,'argv',['make-chapter.py','--content','内容','--output',str(tmp_path),
                                   '--engine',engine,'--reference','old-pack.json'])
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(Path(__file__).resolve().parents[2]/'scripts/make-chapter.py'),run_name='__main__')
    assert error.value.code==2 and 'unrecognized arguments: --reference' in capsys.readouterr().err
