import base64
import math

import httpx

from scidirector_ai.tts.fish import FishAudioTTSProvider, TIMESTAMP_ENDPOINT, marks_from_alignment


def test_fish_keeps_word_times_without_punctuation():
    marks = marks_from_alignment([{"text": "FFN", "start": .1, "end": 1.4},
                                  {"text": "开始", "start": 2.3, "end": 2.8}])
    assert len(marks) == 2
    assert marks[1].start_sec == 2.3
    assert marks[0].end_sec == 1.4
    assert not marks_from_alignment([{"text": "字", "start": math.nan, "end": 1}])


def test_fish_default_requests_and_returns_timestamps(monkeypatch, tmp_path):
    def post(url, **kwargs):
        assert url == TIMESTAMP_ENDPOINT
        return httpx.Response(200, json={"audio_base64": base64.b64encode(b"audio").decode(),
             "alignment": [{"text": "你好", "start": .1, "end": 1.1}]})
    monkeypatch.setattr(httpx, "post", post)
    result = FishAudioTTSProvider(api_key="test").synthesize("你好。", out_path=tmp_path/"test.mp3")
    assert result.marks[0].text == "你好"
    assert result.audio_path.read_bytes() == b"audio"
