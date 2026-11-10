from __future__ import annotations

import base64
import io
import json

import httpx
import pytest
from PIL import Image

from scidirector_ai.config import Settings
from scidirector_ai.illustrations import ImageGenerationError, ImageGenerator, normalize_image, parse_providers
from scidirector_ai.style_matching import recommend_style


def png():
    output = io.BytesIO()
    Image.new('RGBA', (32, 24), (239, 56, 74, 190)).save(output, format='PNG')
    return output.getvalue()


def settings():
    return Settings(_env_file=None, env='test', image_providers_json=json.dumps([
        {'id': 'relay', 'label': 'Relay', 'base_url': 'https://images.example/v1',
         'model': 'custom-image-alias', 'response_format': 'b64_json'},
        {'id': 'second', 'label': 'Second', 'base_url': 'https://second.example/api/v1', 'model': 'other-alias'},
    ]), image_api_keys={'relay': 'test-credential', 'second': 'other-test-credential'})


def test_multiple_provider_catalog_has_no_credentials_or_private_endpoints():
    config = settings()
    catalog = ImageGenerator(config).catalog()
    assert len(catalog['providers']) == 2
    assert all(p['configured'] for p in catalog['providers'])
    assert all('base_url' not in p and 'api_key' not in p for p in catalog['providers'])
    assert 'test-credential' not in json.dumps(catalog)
    assert 'test-credential' not in repr(config)


@pytest.mark.parametrize('provider', ['relay', 'second'])
def test_generation_selects_exact_provider_model_key_and_reencodes_pixels(tmp_path, provider):
    calls = []
    def handle(request):
        body = json.loads(request.content)
        calls.append((request, body))
        return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(png()).decode()}]})
    generator = ImageGenerator(settings(), transport=httpx.MockTransport(handle))
    result = generator.generate(provider_id=provider, prompt='一台蒸汽机', size='1024x1024',
                                output_dir=str(tmp_path), style_source={'content': '清晰尺寸线'})
    request, body = calls[0]
    assert len(calls) == 1
    assert body['n'] == 1 and '清晰尺寸线' in body['prompt']
    assert body['model'] == ('custom-image-alias' if provider == 'relay' else 'other-alias')
    assert request.url.host == ('images.example' if provider == 'relay' else 'second.example')
    assert request.headers['Authorization'] == 'Bearer ' + ('test-credential' if provider == 'relay' else 'other-test-credential')
    assert ('response_format' in body) == (provider == 'relay')
    assert result['provider_id'] == provider
    with Image.open(result['image_path']) as image:
        assert image.size == (32, 24) and image.getpixel((0, 0)) == (239, 56, 74, 190)
        assert image.info == {}


@pytest.mark.parametrize('failure', ['status', 'timeout', 'url', 'empty', 'bad_image'])
def test_upstream_failure_is_sanitized_never_retried_or_saved(tmp_path, failure):
    calls = []
    def handle(request):
        calls.append(request)
        if failure == 'timeout':
            raise httpx.ReadTimeout('test-credential private diagnostics')
        if failure == 'status':
            return httpx.Response(429, text='test-credential upstream diagnostics')
        if failure == 'url':
            return httpx.Response(200, json={'data': [{'url': 'https://images.example/generated.png'}]})
        if failure == 'empty':
            return httpx.Response(200, json={'data': []})
        return httpx.Response(200, json={'data': [{'b64_json': base64.b64encode(b'not an image').decode()}]})
    generator = ImageGenerator(settings(), transport=httpx.MockTransport(handle))
    with pytest.raises(ImageGenerationError) as error:
        generator.generate(provider_id='relay', prompt='一台蒸汽机', size='1024x1024', output_dir=str(tmp_path))
    assert len(calls) == 1
    assert 'test-credential' not in str(error.value)
    assert not list(tmp_path.iterdir())


def test_unconfigured_size_and_busy_fail_before_any_billable_call(tmp_path):
    generator = ImageGenerator(settings(), transport=httpx.MockTransport(lambda request: pytest.fail('must not call')))
    with pytest.raises(ValueError):
        generator.generate(provider_id='unknown', prompt='一台蒸汽机', size='1024x1024', output_dir=str(tmp_path))
    with pytest.raises(ValueError):
        generator.generate(provider_id='relay', prompt='一台蒸汽机', size='500x500', output_dir=str(tmp_path))
    for _ in range(settings().image_max_parallel):
        generator.gate.acquire()
    with pytest.raises(ImageGenerationError, match='未发起新调用'):
        generator.generate(provider_id='relay', prompt='一台蒸汽机', size='1024x1024', output_dir=str(tmp_path))


def test_profiles_validate_duplicates_and_unknown_fields_without_echoing_input():
    rows = json.loads(settings().image_providers_json)
    with pytest.raises(ValueError, match='配置无效'):
        parse_providers(json.dumps(rows + rows))
    rows[0]['unsupported'] = 'test-credential'
    with pytest.raises(ValueError) as error:
        parse_providers(json.dumps(rows))
    assert 'test-credential' not in str(error.value)


def test_normalized_image_preserves_transparency_and_fully_decodes():
    result = normalize_image(png())
    with Image.open(io.BytesIO(result)) as image:
        assert image.mode == 'RGBA' and image.getpixel((0, 0))[3] == 190
    with pytest.raises(ImageGenerationError):
        normalize_image(b'not an image')


def test_normalized_palette_image_preserves_transparency():
    source = Image.new('P', (2, 1))
    source.putpalette([239, 56, 74, 40, 120, 180] + [0] * 762)
    source.putpixel((1, 0), 1)
    encoded = io.BytesIO()
    source.save(encoded, format='PNG', transparency=0)
    with Image.open(io.BytesIO(normalize_image(encoded.getvalue()))) as image:
        assert image.mode == 'RGBA'
        assert image.getpixel((0, 0)) == (239, 56, 74, 0)
        assert image.getpixel((1, 0)) == (40, 120, 180, 255)
        assert image.info == {}


def test_style_match_is_deterministic_uses_catalog_and_has_no_fake_default():
    catalog = {'library_version': 'fixture', 'entries': [
        {'id': 'X17', 'name': '工程蓝图', 'fit': '机械、蒸汽机的原理、结构拆解', 'look': '尺寸线', 'keywords': '技术图解'},
        {'id': 'L9', 'name': '漫画剧场', 'fit': '幽默、搞笑、轻松故事', 'look': '漫画分格', 'keywords': '笑话'},
    ]}
    script = '通过一台蒸汽机的机械结构拆解，逐步讲解它的工作原理。'
    first = recommend_style(script, catalog)
    assert first == recommend_style(script, catalog) and first['style_id'] == 'X17'
    assert first['method'] == 'catalog_keyword_match' and first['reason'].startswith('匹配脚本用词')
    assert recommend_style('轻松幽默的故事，从搞笑漫画的角度讲讲这台机器的趣事。', catalog)['style_id'] == 'L9'
    assert recommend_style('unrelated explanation without matching vocabulary', catalog)['style_id'] == ''


def test_generic_narration_does_not_outweigh_specific_structure_and_principle():
    catalog = {'library_version': 'fixture', 'entries': [
        {'id': 'V7', 'name': '贴纸讲解', 'fit': '轻松的讲解'},
        {'id': 'X17', 'name': '蓝图', 'fit': '讲结构'},
        {'id': 'L36', 'name': '蓝图 · 工程制图', 'fit': '讲原理、讲结构', 'look': '发明家的蓝图自己画出一台接云机器'},
    ]}
    result = recommend_style('通过一台蒸汽机的机械结构拆解，逐步讲解它的工作原理。', catalog)
    assert result['style_id'] == 'L36'
    assert not {'讲解', '一台'} & set(result['matched_terms'])
