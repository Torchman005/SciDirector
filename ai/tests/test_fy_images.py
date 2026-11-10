from __future__ import annotations

from PIL import Image

from scidirector_ai.fy_objects import FyDocument, FyRevision, extract_fy_snapshot
from scidirector_ai.config import Settings
from scidirector_ai.image_assets import image_asset_data
from scidirector_ai.schemas import SceneAsset
from test_fy_main_workflow import LLM, coder, palette, shot
from test_fy_objects import browser, open_page, style
from scidirector_ai.generation_quality import BrowserPreflight


def with_image(style, tmp_path):
    asset_id = 'a' * 32
    folder = tmp_path / 'visuals' / ('b' * 64)
    folder.mkdir(parents=True)
    path = folder / (asset_id + '.png')
    image = Image.new('RGBA', (64, 64), (239, 56, 74, 255))
    image.save(path)
    return style.model_copy(update={'scene_assets': [SceneAsset(asset_id=asset_id, filename='test.png',
        mime_type='image/png', role='prop', width=64, height=64, size_bytes=path.stat().st_size)],
        'scene_asset_sources': {asset_id: str(path)}}), asset_id


def test_main_coder_loads_image_before_seek_and_keeps_it_in_exact_revision(style, browser, tmp_path):
    style, asset_id = with_image(style, tmp_path)
    doc = FyDocument.model_validate({'objects': [{'id': 'engine',
        'box': {'x': .15, 'y': .2, 'width': .3, 'height': .4},
        'keyframes': [{'time': 0}, {'time': 1, 'dx': .1}]}]})
    art = f"object('engine',({{w,h,imageAsset}})=>imageAsset('{asset_id}',0,0,w,h));scene('shot',()=>{{}});"
    llm = LLM([{'palette': palette(), 'document': doc.model_dump(), 'asset_ids': [asset_id], 'code': art}])
    agent = coder(llm, BrowserPreflight(Settings(_env_file=None, env='test',
        llm_provider='openai', sandbox_work_dir=str(tmp_path))), tmp_path)
    result = agent.generate(shot=shot(), style_guide=style)
    assert result.policy_ok, result.policy_summary
    assert result.quality.checked, result.quality.reason
    snapshot = extract_fy_snapshot(result.code)
    assert snapshot.context['image_data'] == image_asset_data(style, [asset_id])
    page = open_page(browser, tmp_path, result.code)
    try:
        frames = []
        for t in [1, 3, 1, 0, 1]:
            page.evaluate('t=>window.__seek(t)', t)
            frames.append(page.evaluate('window.__fyPixels()'))
            assert page.evaluate('window.__fyLayout()') == []
        assert frames[0] == frames[2] == frames[4]
        assert page.evaluate('''() => [...document.getElementById('film').getContext('2d')
          .getImageData(600,400,1,1).data]''') == [239, 56, 74, 255]
    finally:
        page.close()
    original = shot().model_copy(update={'code': result.code})
    revised = agent.apply_exact_fy_revision(shot=original, style_guide=style,
        revision=FyRevision(expect_revision=snapshot.revision, update_objects=[{'id': 'engine', 'layer': 2}]))
    assert revised.policy_ok and revised.skipped_llm
    assert extract_fy_snapshot(revised.code).context['image_data'] == snapshot.context['image_data']
    assert len(llm.calls) == 1
