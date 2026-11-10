"""Real Canvas layout controls; examples are isolated, never published as jobs."""
from __future__ import annotations

import copy
import json

import pytest

from scidirector_ai.fy_objects import FyDocument, FyRevision, apply_fy_revision, extract_fy_snapshot
from scidirector_ai.chapter.fy import compile_fy
from scidirector_ai.fy_generation import shot_timing
from test_fy_main_workflow import LLM, Quality, coder, palette, shot
from test_fy_objects import browser, compile_it, document, open_page, style


def composite(*, role='backdrop', header=False, moving=False):
    doc = FyDocument.model_validate({'objects': [
        {'id': 'panel', 'box': {'x': .06, 'y': .2, 'width': .4, 'height': .4},
         'role': role, 'overlap_group': 'window', 'layer': 99,
         'keyframes': [{'time': .25}, {'time': 1, 'dx': .04}] if moving else []},
        {'id': 'input', 'box': {'x': .12, 'y': .32, 'width': .1, 'height': .14}, 'overlap_group': 'window'},
        {'id': 'output', 'box': {'x': .7, 'y': .32, 'width': .16, 'height': .14}},
    ], 'relations': [{'id': 'feed', 'source': 'input', 'target': 'output',
                      'source_port': 'right', 'target_port': 'left'}]})
    title = "textBlock('TITLE',w*.5,h*.3,w*.48,h*.35,{size:40});" if header else ''
    art = ("object('panel',({ctx,w,h,C,textBlock})=>{ctx.fillStyle=C.gold;ctx.fillRect(0,0,w,h);"+title+"});"
           "object('input',({w,h,label})=>label('IN',w/2,h/2,{size:38}));"
           "object('output',({w,h,label})=>label('OUT',w/2,h/2,{size:38}));scene('shot',()=>{});")
    return doc, art


def test_roles_default_to_solid_and_reject_background_endpoints(style):
    assert all(o.role == 'content' for o in document().objects)
    data = document().model_dump()
    data['objects'][0]['role'] = 'backdrop'
    with pytest.raises(ValueError, match='不能连接背景对象'):
        FyDocument.model_validate(data)
    doc, art = composite(role='content')
    snapshot = extract_fy_snapshot(compile_it(style, doc, art))
    result = apply_fy_revision(snapshot, FyRevision(expect_revision=snapshot.revision,
        update_objects=[{'id': 'panel', 'role': 'backdrop'}]))
    assert result.objects[0].role == 'backdrop'
    assert result.objects[1:] == snapshot.document.objects[1:]
    assert result.relations == snapshot.document.relations


def test_main_coder_carries_layout_apis_and_roles_through_compilation(style, browser, tmp_path):
    doc, art = composite(header=True)
    llm = LLM([{'palette': palette(), 'document': doc.model_dump(), 'code': art}])
    result = coder(llm, Quality(), tmp_path).generate(shot=shot(), style_guide=style)
    assert result.policy_ok and result.llm_attempts == 1
    assert 'textBlock' in llm.calls[0][0] and 'backdrop' in llm.calls[0][0]
    snapshot = extract_fy_snapshot(result.code)
    assert snapshot.document == doc
    assert snapshot.context['object_features'] == ['backdrop-v1', 'text-block-v1']
    page = open_page(browser, tmp_path, result.code)
    try:
        assert page.evaluate('window.__fyLayout()') == []
        assert page.evaluate('window.__fyObjects().relations[0].status') == 'routed'
    finally:
        page.close()


@pytest.mark.parametrize('role', ['content', 'backdrop'])
def test_container_role_controls_route_and_actual_paint_order(style, browser, tmp_path, role):
    doc, art = composite(role=role)
    page = open_page(browser, tmp_path, compile_it(style, doc, art))
    try:
        edge = page.evaluate('window.__fyObjects().relations[0]')
        if role == 'content':
            assert edge['status'] == 'blocked' and not edge['points']
        else:
            assert edge['status'] == 'routed'
            assert page.evaluate('window.__fyLayout()') == []
            # This lies inside the opaque backdrop, beyond the content endpoint.
            # The connector must be painted on top, even with backdrop layer=99.
            assert page.evaluate('''() => {
                const c=document.getElementById('film').getContext('2d');
                const p=c.getImageData(600,421,1,1).data;
                const ref=document.createElement('canvas').getContext('2d');
                ref.fillStyle=FY.C.ink;ref.fillRect(0,0,1,1);
                return [...p].every((v,i)=>v===ref.getImageData(0,0,1,1).data[i]);
            }''')
    finally:
        page.close()


def test_backdrop_text_is_still_an_obstacle_and_seek_is_pure(style, browser, tmp_path):
    doc, art = composite(header=True, moving=True)
    page = open_page(browser, tmp_path, compile_it(style, doc, art))
    try:
        page.evaluate('''() => {
            const ctx=document.getElementById('film').getContext('2d'),draw=ctx.fillText.bind(ctx);
            ctx.fillText=(text,x,y)=>{
                if(text==='TITLE'){
                    const m=ctx.measureText(text),tr=ctx.getTransform();
                    const a=new DOMPoint(x-m.actualBoundingBoxLeft,y-m.actualBoundingBoxAscent).matrixTransform(tr);
                    const b=new DOMPoint(x+m.actualBoundingBoxRight,y+m.actualBoundingBoxDescent).matrixTransform(tr);
                    window.protectedText={left:a.x,top:a.y,right:b.x,bottom:b.y};
                }
                draw(text,x,y);
            };
        }''')
        samples = {}
        for time in (0, 1, 2, 4, 2, 1, 0, 4):
            page.evaluate('t=>{window.protectedText=null;window.__seek(t);}', time)
            assert page.evaluate('window.__fyLayout()') == []
            edge = page.evaluate('window.__fyObjects().relations[0]')
            assert edge['status'] == 'routed'
            if time:
                assert len(edge['points']) > 2
                assert page.evaluate('''() => {
                    const edge=window.__fyObjects().relations[0];
                    const r=window.protectedText;
                    return r && edge.points.slice(1).every((p,i)=>!SCIDRouting.crosses(edge.points[i],p,r));
                }''')
            current = (edge, page.evaluate('window.__fyPixels()'))
            if time in samples:
                assert current == samples[time]
            samples[time] = current
    finally:
        page.close()


def test_backdrop_does_not_exempt_collision_or_unrelated_obstacles(style, browser, tmp_path):
    doc, art = composite()
    data = doc.model_dump()
    # A real content wall still traps the source, despite its shared overlap group.
    data['objects'].append({**copy.deepcopy(data['objects'][0]), 'id': 'wall', 'role': 'content'})
    art = art.replace("scene('shot'", "object('wall',()=>{});scene('shot'")
    page = open_page(browser, tmp_path, compile_it(style, FyDocument.model_validate(data), art))
    try:
        assert page.evaluate('window.__fyObjects().relations[0].status') == 'blocked'
        assert any('wall' in s for s in page.evaluate('window.__fyLayout()'))
    finally:
        page.close()
    data['objects'].pop()
    data['objects'][0]['overlap_group'] = ''
    page = open_page(browser, tmp_path, compile_it(style, FyDocument.model_validate(data), art.replace("object('wall',()=>{});", '')))
    try:
        assert any('重叠' in s for s in page.evaluate('window.__fyLayout()'))
    finally:
        page.close()


TEXT = '智能体 = 能自己干活的AI，Plan carefully! e\u0301 👩‍💻'


@pytest.mark.parametrize('align', ['left', 'center', 'right'])
def test_text_block_preserves_content_and_actual_font_during_nested_scaling(style, browser, tmp_path, align):
    doc = FyDocument.model_validate({'objects': [
        {'id': 'text', 'box': {'x': .2, 'y': .2, 'width': .55, 'height': .4},
         'keyframes': [{'time': 0, 'scale': .6}, {'time': 1, 'scale': 1}]},
    ]})
    art = ("object('text',({ctx,w,h,textBlock})=>{ctx.scale(.8,.8);"
           f"window.layout=textBlock({json.dumps(TEXT, ensure_ascii=False)},0,0,w,h,{{size:58,align:'{align}'}});"
           "});scene('shot',()=>{});")
    page = open_page(browser, tmp_path, compile_it(style, doc, art))
    try:
        seen = {}
        for time in (0, 1, 2, 4, 2, 1, 0):
            page.evaluate('t=>window.__seek(t)', time)
            assert page.evaluate('window.__fyLayout()') == []
            result = page.evaluate('window.layout')
            assert result['text'] == TEXT and ''.join(result['lines']) == TEXT
            assert result['effectiveSize'] >= style.min_font_size
            assert '👩‍💻' in ''.join(result['lines']) and not any(s.endswith('\u200d') for s in result['lines'])
            current = page.evaluate('window.__fyPixels()')
            if time in seen:
                assert current == seen[time]
            seen[time] = current
    finally:
        page.close()


@pytest.mark.parametrize('value', ['first\n\nsecond', 'Supercalifragilisticexpialidocious', '中文标点，换行仍然保留。'])
def test_text_block_explicit_newlines_and_long_tokens(style, browser, tmp_path, value):
    doc = FyDocument.model_validate({'objects': [
        {'id': 'text', 'box': {'x': .2, 'y': .2, 'width': .3, 'height': .4}},
    ]})
    art = (f"object('text',({{w,h,textBlock}})=>{{window.layout=textBlock({json.dumps(value)},0,0,w,h,{{size:48}});}});"
           "scene('shot',()=>{});")
    page = open_page(browser, tmp_path, compile_it(style, doc, art))
    try:
        assert page.evaluate('window.__fyLayout()') == []
        result = page.evaluate('window.layout')
        assert result['text'] == value and ''.join(result['lines']) == value.replace('\n', '')
        if '\n\n' in value:
            assert '' in result['lines']
    finally:
        page.close()


def test_text_block_impossible_fit_fails_without_truncation_or_partial_paint(style, browser, tmp_path):
    doc = FyDocument.model_validate({'objects': [
        {'id': 'text', 'box': {'x': .2, 'y': .2, 'width': .06, 'height': .04}},
    ]})
    art = "object('text',({w,h,textBlock})=>textBlock('完整文字不能截断',0,0,w,h,{size:48}));scene('shot',()=>{});"
    page = open_page(browser, tmp_path, compile_it(style, doc, art))
    try:
        errors = '\n'.join(page.evaluate('window.__errors'))
        assert 'textBlock 放不下完整文字' in errors and '完整文字不能截断' in errors
        assert page.evaluate('window.__fyLayout()') == []  # no half-painted text
    finally:
        page.close()


def test_text_block_font_floor_survives_canvas_font_normalization_each_frame(style, browser, tmp_path):
    doc = FyDocument.model_validate({'objects': [
        {'id': 'text', 'box': {'x': .2, 'y': .2, 'width': .4, 'height': .2},
         'keyframes': [{'time': 0, 'scale': .5}, {'time': 1, 'scale': .7}]},
    ]})
    art = "object('text',({w,h,textBlock})=>textBlock('攻略',0,0,w,h,{size:40}));scene('shot',()=>{});"
    page = open_page(browser, tmp_path, compile_it(style, doc, art))
    try:
        assert page.evaluate('''() => {
            for(let i=0;i<=120;i++){
                window.__seek(i/30);
                const issues=window.__fyLayout();
                if(issues.length)return issues;
            }
            return [];
        }''') == []
    finally:
        page.close()


def test_legacy_chapter_may_keep_its_own_text_block_name(browser, tmp_path):
    art = "const textBlock=()=>label('LEGACY',W/2,H*.4,{size:40});scene('shot',textBlock);"
    html = compile_fy([('shot',art)],shot_timing(shot()),palette=palette(),chapter_chrome=False)
    page = open_page(browser,tmp_path,html)
    try:
        assert page.evaluate('window.__errors') == []
        assert page.evaluate('window.__fyLayout()') == []
    finally:
        page.close()
