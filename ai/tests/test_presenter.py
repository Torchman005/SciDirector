import array
import math
import wave
from pathlib import Path

import pytest

from scidirector_ai.presenter import render_presenter, runtime_paths
from scidirector_ai.presenter_capture import ENVELOPE_HZ, mouth_envelope
from scidirector_ai.config import Settings


def test_mouth_envelope_tracks_speech_energy_and_closes_in_silence(tmp_path: Path) -> None:
    file=tmp_path/"speech.wav"
    samples=array.array("h", [int(12000*math.sin(i*.12)) if 16000<=i<32000 else 0 for i in range(48000)])
    with wave.open(str(file),"wb") as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000); audio.writeframes(samples.tobytes())
    envelope=mouth_envelope(file)
    assert len(envelope)==3*ENVELOPE_HZ
    assert max(envelope[:50])==0
    assert max(envelope[55:95])>.8
    assert max(envelope[115:])==0
    assert all(0<=v<=1 for v in envelope)
    assert mouth_envelope(file)==envelope


def test_missing_runtime_is_explicit_and_never_silently_drops_presenter(tmp_path: Path) -> None:
    settings=Settings(live2d_core_path=str(tmp_path/"missing.js"))
    with pytest.raises(ValueError,match="SCID_LIVE2D_CORE_PATH"):
        runtime_paths(settings)


def test_missing_narration_and_bad_geometry_fail_before_capture(tmp_path: Path,monkeypatch) -> None:
    monkeypatch.setattr("scidirector_ai.presenter.runtime_paths",lambda _: {})
    model=tmp_path/"a.model3.json"; model.write_text("{}")
    kwargs=dict(model_path=str(model),audio_path="",output_dir=str(tmp_path),duration_sec=3,width=240,height=360,fps=30)
    with pytest.raises(ValueError,match="旁白"):
        render_presenter(Settings(),None,**kwargs)
    for key,value in [("duration_sec",float("nan")),("width",2048),("fps",0),("mouth_gain",float("inf"))]:
        with pytest.raises(ValueError):
            render_presenter(Settings(),None,**{**kwargs,key:value})
