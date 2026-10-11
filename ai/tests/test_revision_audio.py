from pathlib import Path
from types import SimpleNamespace

import pytest

from scidirector_ai.config import Settings
from scidirector_ai.schemas import ShotSpec, SceneTag, StyleGuide
from scidirector_ai.service import PipelineService, RenderFailed
from scidirector_ai.tts.base import SynthesisResult, SentenceMark, TTSError, read_marks_sidecar


def test_rerender_returns_fresh_narration_and_timing_or_fails(tmp_path, monkeypatch):
    import scidirector_ai.service as module
    result=SimpleNamespace(video_path=str(tmp_path/'video.mp4'),duration_sec=4,width=1280,height=720,fps=24,
                           engine='motion',render_cost_sec=1,partial_range_honored=False)
    monkeypatch.setattr(module,'build_renderer',lambda *a:SimpleNamespace(render=lambda *a:result))
    monkeypatch.setattr(module,'extract_frames',lambda *a,**k:[])
    class Voice:
        name='test'
        def synthesize(self,text,*,out_path,**kw):
            out_path.parent.mkdir(parents=True,exist_ok=True);out_path.write_bytes(b'audio')
            return SynthesisResult(out_path,3,(SentenceMark(text,.1,2.9),),'test')
    monkeypatch.setattr(module,'build_tts_provider',lambda *a:Voice())
    service=PipelineService(Settings(_env_file=None,llm_provider='mock',tts_provider='none'))
    shot=ShotSpec(tag=SceneTag.MOTION,narration='修改后的旁白。')
    def render():return service._render_shot(job_id='test',shot=shot,code='',style_guide=StyleGuide(),attempt=2,output_dir=str(tmp_path))[0]
    artifact=render()
    assert Path(artifact.audio_path).read_bytes()==b'audio'
    assert read_marks_sidecar(artifact.audio_path)[0].text==shot.narration
    def fail(*a,**kw):raise TTSError('unavailable',retryable=False)
    monkeypatch.setattr(Voice,'synthesize',fail)
    with pytest.raises(RenderFailed,match='配音失败'):render()
