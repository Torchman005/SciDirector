"""Exercise the real main director/coder contract, without paid model calls."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from scidirector_ai.agents.base import style_guide_to_text
from scidirector_ai.agents.coder import CoderAgent
from scidirector_ai.agents.director import DirectorAgent
from scidirector_ai.chapter.fy import PAPER_PALETTE
from scidirector_ai.chapter.fy_styles import list_fy_styles, load_fy_style
from scidirector_ai.config import Settings
from scidirector_ai.fy_generation import extract_fy_code
from scidirector_ai.generation_quality import BrowserPreflight, QualityResult
from scidirector_ai.llm import LLMError
from scidirector_ai.pbconv import style_guide_from_json
from scidirector_ai.schemas import RenderEngine, SceneTag, ShotSpec, StyleGuide


@pytest.fixture
def style(tmp_path, monkeypatch):
    library = tmp_path / 'library'
    directory = library / '风格提示词' / 'X_测试'
    directory.mkdir(parents=True)
    (directory / 'X17_蓝图.md').write_text('# X17 蓝图\n- 长什么样：蓝底、白色尺寸线与黄色标注\n- 适合：技术原理\n完整画风原文。', encoding='utf-8')
    monkeypatch.setenv('FY_STYLE_LIBRARY_ROOT', str(library))
    source = load_fy_style('X17')
    return StyleGuide(animation_style='fy', fy_style_id='X17', fy_style_source=source)


def scene_code(text='输入'):
    return 'theme(' + json.dumps({**PAPER_PALETTE, 'paper': '#17508c', 'ink': '#ffffff'}) + ");scene('shot', ({ctx,t,M,C,W,H,label})=>{label('" + text + "', W*.4+M.progress(t,0,4)*30,H*.4,{size:40,color:C.ink});});"


def shot():
    return ShotSpec(shot_id='job-fy-s000', index=0, tag=SceneTag.MOTION, engine=RenderEngine.MOTION,
                    narration='输入经过处理得到输出。', visual_brief='用蓝图线条呈现处理过程。', duration_sec=4,
                    meta={'generation_mode': 'fy-canvas', 'fy_style_id': 'X17'})


class LLM:
    usage = SimpleNamespace(total_tokens=0)

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def chat_json(self, system, user, schema, **kwargs):
        self.calls.append((system, user))
        return schema.model_validate(self.replies.pop(0))


class Quality:
    def __init__(self, failures=0):
        self.failures, self.codes = failures, []

    def check(self, code, *args):
        self.codes.append(code)
        return QualityResult(checked=True, issues=['文字重叠，请重新布局'] if len(self.codes) <= self.failures else [])


class NoLegacyExamples:
    def retrieve(self, *args, **kwargs):
        pytest.fail('fy must never retrieve legacy examples')


def coder(llm, checker, tmp_path):
    return CoderAgent(llm, Settings(env='test', llm_provider='mock', sandbox_work_dir=str(tmp_path)),
                      retriever=NoLegacyExamples(), quality_checker=checker)


def test_catalog_and_source_use_same_reader_from_any_cwd(style, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    catalog = list_fy_styles()
    assert catalog['prompt_count'] == 1
    assert catalog['entries'][0]['look'] == '蓝底、白色尺寸线与黄色标注'
    assert catalog['library_version'] == style.fy_style_source['library_version']
    with pytest.raises(ValueError, match='STYLE_NOT_FOUND'):
        load_fy_style('X999')


def test_snapshot_roundtrip_is_strict_and_conflicting_prompts_are_absent(style):
    restored = style_guide_from_json(style.model_dump_json())
    assert restored.fy_style_source == style.fy_style_source
    text = style_guide_to_text(restored)
    assert '完整画风原文。' in text and 'X17' in text
    assert 'paper_stage' not in text and '二次元' not in text and '#4F8CFF' not in text
    payload = style.model_dump()
    payload['fy_style_source']['content'] += '改动'
    with pytest.raises(ValueError, match='SHA-256'):
        style_guide_from_json(json.dumps(payload))
    with pytest.raises(ValueError, match='完整画风来源'):
        style_guide_from_json('{"animation_style":"fy","fy_style_id":"X17"}')


def test_main_director_routes_every_tag_to_fy(style):
    llm = LLM([{'shots': [{'tag': tag, 'narration': '输入产生输出。', 'visual_brief': '蓝图尺寸线', 'duration_sec': 2}
                          for tag in ['MATH', 'DATA', 'CODE']]}])
    plan = DirectorAgent(llm).plan(job_id='fy', raw_script='输入产生输出。', style_guide=style, target_duration_sec=6)
    assert len(plan.shots) == 3
    assert all(s.engine is RenderEngine.MOTION and s.meta['generation_mode'] == 'fy-canvas' for s in plan.shots)
    assert all(s.meta['fy_style_sha256'] == style.fy_style_source['sha256'] for s in plan.shots)
    assert '完整画风原文。' in llm.calls[0][1]


def test_main_coder_and_repair_use_full_source_and_only_regenerate_scene(style, tmp_path):
    llm = LLM([{'code': scene_code()}, {'code': scene_code('输出')}])
    checker = Quality(failures=1)
    result = coder(llm, checker, tmp_path).generate(shot=shot(), style_guide=style)
    assert result.policy_ok and result.llm_attempts == 2
    assert extract_fy_code(result.code) == scene_code('输出')
    assert 'function createCanvasContract' in result.code
    assert 'id="sub"' not in result.code and 'id="tag"' not in result.code
    assert 'scid-fy-source' in result.code and style.fy_style_source['sha256'] in result.code
    for _, user in llm.calls:
        assert '完整画风原文。' in user and 'X17' in user
    assert '文字重叠' in llm.calls[1][1]
    repair = coder(LLM([{'code': scene_code()}]), Quality(), tmp_path).generate(
        shot=shot(), style_guide=style, attempt=2, previous_code=result.code, feedback_text='移动标注')
    assert repair.policy_ok


def test_explicit_fy_never_falls_back_to_stock_or_legacy_templates(style, tmp_path):
    agent = coder(LLM([]), Quality(), tmp_path)
    with pytest.raises(LLMError, match='不能回落'):
        agent.generate(shot=shot().model_copy(update={'engine': RenderEngine.STOCK}), style_guide=style)
    with pytest.raises(LLMError, match='缺少完整画风来源'):
        agent.generate(shot=shot(), style_guide=StyleGuide())


def test_grpc_catalog_source_and_invalid_fy_requests(style):
    import grpc
    from concurrent.futures import ThreadPoolExecutor
    from scidirector_ai.grpc_server import AiDirectorServicer
    from scidirector.v1 import ai_service_pb2 as pb, ai_service_pb2_grpc as rpc
    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    rpc.add_AiDirectorServiceServicer_to_server(AiDirectorServicer(None), server)
    port = server.add_insecure_port('127.0.0.1:0')
    server.start()
    try:
        with grpc.insecure_channel(f'127.0.0.1:{port}') as channel:
            client = rpc.AiDirectorServiceStub(channel)
            catalog = json.loads(client.GetFyStyles(pb.FyStylesRequest(), timeout=5).catalog_json)
            source = json.loads(client.GetFyStyles(pb.FyStylesRequest(style_id='X17'), timeout=5).source_json)
            assert catalog['prompt_count'] == 1 and source == style.fy_style_source
            with pytest.raises(grpc.RpcError) as exc:
                client.PlanScript(pb.PlanScriptRequest(job_id='invalid-fy', raw_script='输入得到输出。',
                    style_guide_json='{"animation_style":"fy","fy_style_id":"X17"}'), timeout=5)
            assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    finally:
        server.stop(0).wait()


@pytest.mark.skipif(not os.environ.get('SCID_CHROME'), reason='Set SCID_CHROME for sandbox browser checks')
def test_main_sandbox_preflight_reads_actual_canvas_text(style, tmp_path, monkeypatch):
    settings = Settings(env='test', llm_provider='mock', sandbox_work_dir=str(tmp_path))
    # Exercise the sandbox check while keeping all model calls stubbed.
    monkeypatch.setattr(Settings, 'text_target', lambda self: SimpleNamespace(provider='openai'))
    from scidirector_ai.chapter.fy import compile_fy
    from scidirector_ai.fy_generation import shot_timing
    good = compile_fy([('shot', scene_code())], shot_timing(shot()), style=style.fy_style_source, chapter_chrome=False, min_font=32)
    checker = BrowserPreflight(settings)
    result = checker.check(good, shot(), style)
    assert result.checked and not result.issues, result
    bad = compile_fy([('shot', scene_code().replace('{size:40,color:C.ink});',
                     "{size:40,color:C.ink});label('重叠', W*.4+M.progress(t,0,4)*30,H*.4,{size:40});"))],
                     shot_timing(shot()), style=style.fy_style_source, chapter_chrome=False, min_font=32)
    result = checker.check(bad, shot(), style)
    assert result.checked and any('文字重叠' in x for x in result.issues), result
