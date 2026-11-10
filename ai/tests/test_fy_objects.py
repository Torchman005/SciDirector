from __future__ import annotations

import copy
import json
import os
from types import SimpleNamespace

import pytest

from scidirector_ai.agents.coder import CoderAgent
from scidirector_ai.browser_preflight import inspect
from scidirector_ai.chapter.fy import compile_fy
from scidirector_ai.config import Settings
from scidirector_ai.fy_generation import extract_fy_code, extract_fy_palette, shot_timing
from scidirector_ai.fy_objects import (FY_REVISION_PREFIX, FyDocument, FyRevision, apply_fy_revision,
    document_changes, extract_fy_snapshot, parse_fy_revision)
from scidirector_ai.scene_revision import review_manifest
from scidirector_ai.schemas import CriticFeedback, RepairTask, RenderArtifact
from scidirector_ai.service import PipelineService, RenderFailed
from test_fy_main_workflow import LLM, Quality, palette, shot, style


ART = """
object('input',({ctx,w,h,label,C})=>{
  ctx.fillStyle=C.paper;ctx.strokeStyle=C.line;ctx.lineWidth=3;
  ctx.beginPath();ctx.roundRect(3,3,w-6,h-6,6);ctx.fill();ctx.stroke();
  label('INPUT',w/2,h/2,{size:38});
});
object('rotor',({ctx,w,h,t,label,C})=>{
  ctx.save();ctx.translate(w/2,h/2);ctx.rotate(t*.4);ctx.strokeStyle=C.gold;ctx.lineWidth=5;
  ctx.beginPath();ctx.ellipse(0,0,w/2-3,h/2-3,-t*.4,0,Math.PI*2);ctx.stroke();
  for(let i=0;i<6;i++){ctx.rotate(Math.PI/3);ctx.beginPath();ctx.moveTo(12,0);ctx.lineTo(Math.min(w,h)*.38,0);ctx.stroke();}
  ctx.restore();ctx.fillStyle=C.paper;ctx.fillRect(w*.15,h*.38,w*.7,h*.24);
  label('PROCESS',w/2,h/2,{size:38});
});
object('output',({ctx,w,h,label,C})=>{
  ctx.strokeStyle=C.ink;ctx.lineWidth=3;ctx.strokeRect(3,3,w-6,h-6);
  label('OUTPUT',w/2,h/2,{size:38});
});
scene('shot',()=>{});
"""


def document():
    return FyDocument.model_validate({'objects': [
        {'id': 'input', 'box': {'x': .07, 'y': .32, 'width': .19, 'height': .22}},
        {'id': 'rotor', 'box': {'x': .4, 'y': .28, 'width': .22, 'height': .3},
         'keyframes': [{'time': 0}, {'time': 1, 'dy': .08}]},
        {'id': 'output', 'box': {'x': .77, 'y': .32, 'width': .16, 'height': .22}},
    ], 'relations': [
        {'id': 'feed', 'source': 'input', 'target': 'rotor', 'source_port': 'right', 'target_port': 'left'},
        {'id': 'result', 'source': 'rotor', 'target': 'output', 'source_port': 'right', 'target_port': 'left'},
    ]})


def compile_it(style, doc=None, art=ART):
    return compile_fy([('shot', art)], shot_timing(shot()), style=style.fy_style_source,
        chapter_chrome=False, min_font=style.min_font_size, palette=palette(), document=doc or document())


def move(snapshot):
    return FyRevision(expect_revision=snapshot.revision, update_objects=[
        {'id': 'rotor', 'box': {'x': .43, 'y': .15, 'width': .22, 'height': .3}}])


def test_exact_move_preserves_other_objects_source_palette_timing_and_narration(style, tmp_path):
    original = compile_it(style)
    snapshot = extract_fy_snapshot(original)
    request = move(snapshot)
    llm = LLM([])
    agent = CoderAgent(llm, Settings(_env_file=None, llm_provider='mock'), quality_checker=Quality())
    source = shot().model_copy(update={'code': original})
    result = agent.apply_exact_fy_revision(shot=source, style_guide=style, revision=request)
    after = extract_fy_snapshot(result.code)
    assert result.skipped_llm and result.llm_attempts == 0 and not llm.calls
    assert after.revision != snapshot.revision
    assert after.code == snapshot.code == extract_fy_code(original)
    assert extract_fy_palette(result.code) == extract_fy_palette(original)
    for key in snapshot.context.keys() - {'object_document'}:
        assert after.context[key] == snapshot.context[key]
    assert after.document.objects[0] == snapshot.document.objects[0]
    assert after.document.objects[2] == snapshot.document.objects[2]
    assert after.document.relations == snapshot.document.relations
    assert [c['id'] for c in document_changes(snapshot.document, after.document)] == ['rotor']
    marker = json.loads(result.code.split('<script id="scid-fy-data" type="application/json">')[1].split('</script>')[0])
    assert marker['parent_revision'] == snapshot.revision and marker['changes'][0]['id'] == 'rotor'
    assert source.narration == shot().narration and source.code == original
    assert result.code.count('id="scid-fy-code"') == 1
    assert 'fy 对象与关系规格' in review_manifest(result.code) and '"source": "input"' in review_manifest(result.code)
    with pytest.raises(ValueError, match='版本冲突'):
        agent.apply_exact_fy_revision(shot=source.model_copy(update={'code': result.code}), style_guide=style, revision=request)


def test_version_guards_noops_invalid_geometry_and_references(style):
    snapshot = extract_fy_snapshot(compile_it(style))
    for data, message in [
        ({'expect_revision': '0'*64}, '版本冲突'),
        ({'expect_revision': snapshot.revision, 'explanation': 'done'}, '没有实际变化'),
        ({'expect_revision': snapshot.revision, 'update_objects': [{'id': 'missing', 'layer': 1}]}, '不存在'),
        ({'expect_revision': snapshot.revision, 'remove_relations': ['missing']}, '不存在'),
        ({'expect_revision': snapshot.revision, 'upsert_relations': [
            {'id': 'bad', 'source': 'input', 'target': 'missing'}]}, '不存在'),
    ]:
        with pytest.raises(ValueError, match=message):
            apply_fy_revision(snapshot, FyRevision.model_validate(data))
    for data in [
        {'update_objects': [{'id': 'input'}]},
        {'update_objects': [{'id': 'input', 'box': {'x': .9, 'y': .1, 'width': .2, 'height': .2}}]},
        {'update_objects': [{'id': 'input', 'layer': 1}, {'id': 'input', 'layer': 2}]},
    ]:
        with pytest.raises(ValueError):
            FyRevision.model_validate({'expect_revision': snapshot.revision, **data})
    bad = document().model_dump()
    bad['objects'][1]['keyframes'] = [{'time': .8}, {'time': .2}]
    with pytest.raises(ValueError, match='严格递增'):
        FyDocument.model_validate(bad)
    bad['objects'][0]['box']['x'] = float('nan')
    with pytest.raises(ValueError):
        FyDocument.model_validate(bad)
    with pytest.raises(ValueError):
        parse_fy_revision(FY_REVISION_PREFIX+'{')
    assert parse_fy_revision('请移动对象') is None


def test_snapshot_detects_tampering_and_contract_changes(style):
    original = compile_it(style)
    with pytest.raises(ValueError, match='哈希不一致'):
        extract_fy_snapshot(original.replace('INPUT', 'ALTERED'))
    palette_tag = '<script id="scid-fy-palette" type="application/json">'
    palette_start = original.index(palette_tag) + len(palette_tag)
    palette_end = original.index('</script>', palette_start)
    wrong_palette = json.dumps({**palette(), 'ink': '#fff'})
    with pytest.raises(ValueError, match='配色来源'):
        extract_fy_snapshot(original[:palette_start]+wrong_palette+original[palette_end:])
    previous = extract_fy_snapshot(original)
    source = shot().model_copy(update={'code': original, 'narration': '不同的旁白。'})
    agent = CoderAgent(LLM([]), Settings(_env_file=None, llm_provider='mock'), quality_checker=Quality())
    with pytest.raises(ValueError, match='已变化'):
        agent.apply_exact_fy_revision(shot=source, style_guide=style, revision=move(previous))


def test_relation_only_revision_and_legacy_edit_rejection(style):
    original = compile_it(style)
    snapshot = extract_fy_snapshot(original)
    result = apply_fy_revision(snapshot, FyRevision(expect_revision=snapshot.revision,
        upsert_relations=[{'id': 'feed', 'source': 'rotor', 'target': 'input',
                           'source_port': 'left', 'target_port': 'right'}], remove_relations=['result']))
    assert result.objects == snapshot.document.objects
    assert result.relations[0].source == 'rotor' and len(result.relations) == 1
    assert {c['id'] for c in document_changes(snapshot.document, result)} == {'feed', 'result'}
    legacy = compile_fy([('shot', "const object = {};scene('shot',()=>{});")], shot_timing(shot()),
                        palette=palette(), chapter_chrome=False)
    assert extract_fy_snapshot(legacy) is None
    agent = CoderAgent(LLM([]), Settings(_env_file=None, llm_provider='mock'), quality_checker=Quality())
    with pytest.raises(ValueError, match='没有对象文档'):
        agent.apply_exact_fy_revision(shot=shot().model_copy(update={'code': legacy}),
                                     style_guide=style, revision=move(snapshot))


def test_exact_fy_review_failure_does_not_widen_edit_or_change_voice(style, monkeypatch):
    settings = Settings(_env_file=None, llm_provider='mock', tts_provider='none')
    svc = PipelineService(settings)
    llm = LLM([])
    svc._coder = CoderAgent(llm, settings, quality_checker=Quality())
    code = compile_it(style)
    calls = []
    def render(**kwargs):
        calls.append(kwargs)
        return RenderArtifact(video_path='edited.mp4', audio_path=kwargs['speech_path']), False
    monkeypatch.setattr(svc, '_render_shot', render)
    issue = CriticFeedback(passed=False, suggestions=['核对图示'], repair_tasks=[RepairTask(task_id='r1', target='#rotor', frame_indices=[1],
        evidence='仍需核对', instruction='核对图示', acceptance='图示正确')])
    svc._critic = SimpleNamespace(review=lambda **kw: SimpleNamespace(feedback=issue, degraded=False))
    request = FY_REVISION_PREFIX + move(extract_fy_snapshot(code)).model_dump_json()
    updated, artifact, feedback, _ = svc.revise_shot(job_id='test', shot=shot().model_copy(update={'code': code}),
        style_guide=style, human_comment=request, attempt=2, previous_artifact=RenderArtifact(audio_path='original.mp3'))
    assert len(calls) == 1 and not llm.calls and not feedback.passed
    assert artifact.audio_path == 'original.mp3' and updated.narration == shot().narration
    assert '未扩大修改' in feedback.handoff_reason
    def broken(**kw):
        raise RenderFailed('测试渲染故障')
    monkeypatch.setattr(svc, '_render_shot', broken)
    with pytest.raises(RenderFailed, match='第 1/1 轮停止'):
        svc.revise_shot(job_id='test', shot=shot().model_copy(update={'code': code}),
            style_guide=style, human_comment=request, attempt=2)
    assert not llm.calls


@pytest.fixture
def browser():
    if not os.environ.get('SCID_CHROME'):
        pytest.skip('Set SCID_CHROME for real Canvas object tests')
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ['SCID_CHROME'], args=['--disable-gpu'])
        yield browser
        browser.close()


def open_page(browser, tmp_path, code):
    path = tmp_path / 'index.html'
    path.write_text(code, encoding='utf-8')
    page = browser.new_page(viewport={'width': 1920, 'height': 1080})
    page.goto(path.resolve().as_uri())
    page.wait_for_function('window.__ready === true')
    return page


def test_real_canvas_endpoints_follow_move_and_reverse_repeat_seek(style, browser, tmp_path):
    code = compile_it(style)
    snapshot = extract_fy_snapshot(code)
    after = compile_it(style, apply_fy_revision(snapshot, move(snapshot)))
    geometries = []
    for index, html in enumerate((code, after)):
        page = open_page(browser, tmp_path, html)
        try:
            pixels = {}
            for t in [0, 1, 2, 4, 2, 1, 0, 4, 4]:
                page.evaluate('t=>window.__seek(t)', t)
                assert page.evaluate('window.__fyLayout()') == []
                state = page.evaluate('window.__fyObjects()')
                by_id = {o['id']: o for o in state['objects']}
                for edge in state['relations']:
                    assert edge['status'] == 'routed'
                    source, target = by_id[edge['source']]['rect'], by_id[edge['target']]['rect']
                    assert edge['points'][0]['x'] == pytest.approx(source['right'])
                    assert edge['points'][0]['y'] == pytest.approx((source['top']+source['bottom'])/2)
                    assert edge['points'][-1]['x'] == pytest.approx(target['left'])
                    assert edge['points'][-1]['y'] == pytest.approx((target['top']+target['bottom'])/2)
                current = page.evaluate('window.__fyPixels()')
                if t in pixels:
                    assert pixels[t] == current
                pixels[t] = current
            geometries.append(state)
            page.screenshot(path=str(tmp_path / f'preview-{index}.png'))
        finally:
            page.close()
    assert geometries[0]['objects'][0] == geometries[1]['objects'][0]
    assert geometries[0]['objects'][2] == geometries[1]['objects'][2]
    assert geometries[0]['relations'][0]['points'] != geometries[1]['relations'][0]['points']


def test_real_canvas_reports_overlap_blocked_paths_missing_painters_and_clipped_text(style, browser, tmp_path):
    doc = document().model_dump()
    doc['objects'][1]['box'] = copy.deepcopy(doc['objects'][0]['box'])
    doc['objects'][1]['keyframes'] = []
    page = open_page(browser, tmp_path, compile_it(style, FyDocument.model_validate(doc)))
    try:
        issues = page.evaluate('window.__fyLayout()')
        assert any('重叠' in issue for issue in issues)
        assert any('无可用路径' in issue for issue in issues)
        assert any(e['status'] == 'blocked' and not e['points'] for e in page.evaluate('window.__fyObjects().relations'))
    finally:
        page.close()
    for art, expected in [(ART.replace("object('input'", "object('unknown'"), '对象注册无效'),
                          (ART[ART.index("object('rotor'"):], '对象未注册'),
                          (ART.replace("w/2,h/2,{size:38}", "-20,h/2,{size:38}"), '文字超出对象边界')]:
        page = open_page(browser, tmp_path, compile_it(style, art=art))
        try:
            if expected in {'对象注册无效', '对象未注册'}:
                assert expected in '\n'.join(page.evaluate('window.__errors'))
            else:
                assert any(expected in issue for issue in page.evaluate('window.__fyLayout()'))
        finally:
            page.close()


def test_real_canvas_routes_around_obstacle_and_presenter(style, browser, tmp_path):
    doc = document().model_dump()
    doc['objects'][1]['box'] = {'x': .4, 'y': .32, 'width': .22, 'height': .22}
    doc['objects'][1]['keyframes'] = [{'time': 0}, {'time': 1, 'dy': -.2}]
    doc['relations'] = [{'id': 'bypass', 'source': 'input', 'target': 'output',
                         'source_port': 'right', 'target_port': 'left'}]
    html = compile_fy([('shot', ART)], shot_timing(shot()), palette=palette(), chapter_chrome=False,
        document=FyDocument.model_validate(doc), presenter_area={'x': .68, 'y': .55, 'width': .32, 'height': .45})
    page = open_page(browser, tmp_path, html)
    try:
        edge = page.evaluate('window.__fyObjects().relations[0]')
        assert edge['status'] == 'routed' and len(edge['points']) > 2
        assert page.evaluate('''() => {
            const state=window.__fyObjects(),edge=state.relations[0],r=state.objects[1].rect;
            return edge.points.slice(1).every((p,i)=>!SCIDRouting.crosses(edge.points[i],p,r));
        }''')
        page.evaluate('window.__seek(4)')
        later = page.evaluate('window.__fyObjects().relations[0]')
        assert later['status'] == 'routed' and len(later['points']) == 2
        assert later['points'] != edge['points']
        page.evaluate('window.__seek(0)')
        assert page.evaluate('window.__fyObjects().relations[0]') == edge
    finally:
        page.close()


def test_hidden_endpoint_hides_relation_without_blocked_error(style, browser, tmp_path):
    doc = document().model_dump()
    doc['objects'][0]['keyframes'] = [{'time': .25}]
    page = open_page(browser, tmp_path, compile_it(style, FyDocument.model_validate(doc)))
    try:
        assert page.evaluate('window.__fyObjects().relations[0].status') == 'hidden'
        assert not any('无可用路径' in issue for issue in page.evaluate('window.__fyLayout()'))
        page.evaluate('window.__seek(2)')
        assert page.evaluate('window.__fyObjects().relations[0].status') == 'routed'
        page.evaluate('window.__seek(0)')
        assert page.evaluate('window.__fyObjects().relations[0].status') == 'hidden'
    finally:
        page.close()


def test_legacy_local_object_variable_still_compiles(style, browser, tmp_path):
    art = "const object = {text:'LEGACY'};scene('shot',()=>label(object.text,W*.5,H*.4,{size:40}));"
    html = compile_fy([('shot', art)], shot_timing(shot()), palette=palette(), chapter_chrome=False)
    page = open_page(browser, tmp_path, html)
    try:
        assert page.evaluate('window.__errors') == []
        page.evaluate('window.__seek(2)')
        assert page.evaluate('window.__fyLayout()') == []
    finally:
        page.close()


def test_preflight_catches_brief_crossing_between_sparse_samples(style, tmp_path):
    if not os.environ.get('SCID_CHROME'):
        pytest.skip('Set SCID_CHROME for actual frame-cadence preflight')
    doc = document().model_dump()
    # A brief crossing at t=.90s, between the 0.875 and 1.0 raster probes.
    doc['objects'] = [
        {'id': 'input', 'box': {'x': .1, 'y': .3, 'width': .05, 'height': .1},
         'keyframes': [{'time': 0}, {'time': .21875}, {'time': .23125, 'dx': .6, 'easing': 'linear'},
                       {'time': 1, 'dx': .6}]},
        {'id': 'output', 'box': {'x': .4, 'y': .3, 'width': .05, 'height': .1}},
    ]
    doc['relations'] = []
    art = "object('input',({ctx,w,h})=>ctx.fillRect(0,0,w,h));object('output',({ctx,w,h})=>ctx.fillRect(0,0,w,h));scene('shot',()=>{});"
    path = tmp_path / 'scan.html'
    path.write_text(compile_it(style, FyDocument.model_validate(doc), art=art), encoding='utf-8')
    result = inspect(path, 1920, 1080, 4, 32, executable=os.environ['SCID_CHROME'])
    assert any('0.900s' in issue and '重叠' in issue for issue in result['issues']), result


def test_font_diagnostics_report_owner_and_total_scale_with_stable_key(style, browser, tmp_path):
    doc = document().model_dump()
    doc['objects'][0]['keyframes'] = [{'time': 0, 'scale': .7}, {'time': 1, 'scale': .8}]
    page = open_page(browser, tmp_path, compile_it(style, FyDocument.model_validate(doc)))
    try:
        failures = []
        for time in (0, 2, 4, 0):
            page.evaluate('t=>window.__seek(t)', time)
            details = page.evaluate('window.__fyLayoutDetails()')
            failures.append(next(d for d in details if json.loads(d['key'])[:3] == ['font-size','shot','input']))
            assert page.evaluate('window.__fyLayout()') == [d['message'] for d in details]
        assert len({d['key'] for d in failures}) == 1
        assert failures[0] == failures[-1]
        assert '26.6px' in failures[0]['message'] and '38.0px' in failures[0]['message']
        assert '0.700' in failures[0]['message'] and '[对象 input]' in failures[0]['message']
        assert failures[0]['message'] != failures[1]['message']
    finally:
        page.close()


@pytest.mark.parametrize('port', ['right', 'auto'])
def test_blocked_relation_names_enclosing_object_without_exempting_group(style, browser, tmp_path, port):
    doc = document().model_dump()
    doc['objects'][0].update(box={'x': .12, 'y': .3, 'width': .1, 'height': .15}, overlap_group='container')
    doc['objects'][1].update(box={'x': .07, 'y': .2, 'width': .3, 'height': .4},
                             overlap_group='container', keyframes=[])
    doc['relations'] = [{'id': 'feed', 'source': 'input', 'target': 'output',
                         'source_port': port, 'target_port': 'left'}]
    art = ("object('input',({w,h,label})=>label('IN',w/2,h/2,{size:38}));"
           "object('rotor',({ctx,w,h})=>ctx.strokeRect(1,1,w-2,h-2));"
           "object('output',({w,h,label})=>label('OUT',w/2,h/2,{size:38}));scene('shot',()=>{});")
    page = open_page(browser, tmp_path, compile_it(style, FyDocument.model_validate(doc), art))
    try:
        details = page.evaluate('window.__fyLayoutDetails()')
        failure = next(d for d in details if json.loads(d['key']) == ['route-blocked','feed'])
        assert f'input.{port} 位于 rotor 内' in failure['message']
        assert 'overlap_group' in failure['message']
        assert page.evaluate('window.__fyObjects().relations[0].status') == 'blocked'
        assert page.evaluate('window.__fyObjects().relations[0].points') == []
    finally:
        page.close()


def test_preflight_repeated_defect_does_not_hide_late_different_failure(style, tmp_path):
    if not os.environ.get('SCID_CHROME'):
        pytest.skip('Set SCID_CHROME for actual deduplication preflight')
    doc = document().model_dump()
    doc['objects'] = [doc['objects'][0], {**doc['objects'][2], 'keyframes': [{'time': .75}]}]
    doc['relations'] = []
    art = ("object('input',({w,h,label})=>label('SMALL',w/2,h/2,{size:20}));"
           "object('output',({label})=>label('CLIPPED',0,0,{size:38}));scene('shot',()=>{});")
    path = tmp_path / 'diagnostics.html'
    path.write_text(compile_it(style, FyDocument.model_validate(doc), art), encoding='utf-8')
    result = inspect(path, 1920, 1080, 4, 32, executable=os.environ['SCID_CHROME'])
    assert len([i for i in result['issues'] if '文字字号过小' in i and 'SMALL' in i]) == 1, result
    clipped = next(i for i in result['issues'] if '[对象 output]' in i and '文字超出对象边界' in i)
    assert clipped.startswith('3.00s') and '(w/2,h/2)' in clipped, result
    assert '对象边界' in clipped and 'px' in clipped


@pytest.mark.parametrize('overflow_pixels', [0, .01])
def test_exact_safe_area_boundary_tolerates_only_arithmetic_noise(style, browser, tmp_path, overflow_pixels):
    doc = document().model_dump()
    doc['objects'] = [{'id': 'input', 'box': {
        'x': .3, 'y': .76 + overflow_pixels/1080, 'width': .4, 'height': .09}}]
    doc['relations'] = []
    art = "object('input',({w,h,label})=>label('CAPTION',w/2,h/2,{size:38}));scene('shot',()=>{});"
    page = open_page(browser, tmp_path, compile_it(style, FyDocument.model_validate(doc), art))
    try:
        issues = page.evaluate('window.__fyLayout()')
        if overflow_pixels:
            assert any('[对象 input] 出画或进入字幕区域' in issue for issue in issues)
        else:
            assert not issues
    finally:
        page.close()
