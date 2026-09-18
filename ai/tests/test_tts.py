"""TTS 适配层测试。

分两类：

* **协议/适配器形状**（不联网）：请求头与请求体是否按各家的约定构造、
  失败是否正确**分类**（可重试 vs 不可重试）、异常响应是否被拦下而不是写成坏文件；
* **Edge TTS 真实合成**（联网，缺网自动跳过）：这是四家里本机唯一能真跑的 ——
  实测它会返回**句级**时间戳，因此这条用例同时钉住「时间戳真的拿到并落了盘」。

其余三家（豆包 / OpenAI / Fish）本机既无凭据、端点也不通，**没有端到端验证**，
只覆盖到「请求形状与错误分类」这一层。这个边界必须在这里写明，
否则读测试的人会以为它们跑通过。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from scidirector_ai.config import Settings
from scidirector_ai.tts.base import (
    MARKS_FORMAT_VERSION,
    SentenceMark,
    SynthesisResult,
    TTSError,
    marks_sidecar_path,
    read_marks_sidecar,
    write_marks_sidecar,
)
from scidirector_ai.tts.doubao import SUCCESS_CODE, DoubaoTTSProvider
from scidirector_ai.tts.edge import EdgeTTSProvider
from scidirector_ai.tts.factory import build_tts_provider
from scidirector_ai.tts.fish import FishAudioTTSProvider


# ---------------------------------------------------------------------------
# 时间戳 sidecar：这是与 Go 侧的契约
# ---------------------------------------------------------------------------


class TestMarksSidecar:
    def test_round_trip(self, tmp_path: Path) -> None:
        audio = tmp_path / "shot.mp3"
        audio.write_bytes(b"fake")
        result = SynthesisResult(
            audio_path=audio,
            duration_sec=7.15,
            marks=(
                SentenceMark("第一句。", 0.1, 2.225),
                SentenceMark("第二句。", 2.275, 2.538),
            ),
            provider="edge",
        )

        side = write_marks_sidecar(audio, result)
        assert side == marks_sidecar_path(audio)
        assert side.name == "shot.mp3.marks.json"

        back = read_marks_sidecar(audio)
        assert len(back) == 2
        assert back[0].text == "第一句。"
        assert back[0].start_sec == pytest.approx(0.1)
        assert back[0].end_sec == pytest.approx(2.325)

    def test_writes_payload_shape_go_will_read(self, tmp_path: Path) -> None:
        """钉住 JSON 字段名与版本号 —— Go 侧按这两个东西解析。"""
        audio = tmp_path / "a.mp3"
        audio.write_bytes(b"x")
        write_marks_sidecar(
            audio, SynthesisResult(audio_path=audio, duration_sec=1.0, provider="edge")
        )
        payload = json.loads(marks_sidecar_path(audio).read_text(encoding="utf-8"))
        assert payload["version"] == MARKS_FORMAT_VERSION
        assert set(payload) >= {"version", "provider", "duration_sec", "marks"}

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """没有 sidecar → 空元组，调用方回退到按镜头时长对齐。"""
        assert read_marks_sidecar(tmp_path / "nope.mp3") == ()

    def test_unknown_version_is_ignored(self, tmp_path: Path) -> None:
        """版本不认识时**忽略**而不是猜着解析：宁可回退，也不要读出半截错数据。"""
        audio = tmp_path / "b.mp3"
        audio.write_bytes(b"x")
        marks_sidecar_path(audio).write_text(
            json.dumps({"version": 999, "marks": [{"text": "x", "start_sec": 0, "duration_sec": 1}]}),
            encoding="utf-8",
        )
        assert read_marks_sidecar(audio) == ()

    def test_corrupt_file_returns_empty(self, tmp_path: Path) -> None:
        audio = tmp_path / "c.mp3"
        audio.write_bytes(b"x")
        marks_sidecar_path(audio).write_text("{ 不是 JSON", encoding="utf-8")
        assert read_marks_sidecar(audio) == ()

    def test_broken_entry_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        """单条坏记录只跳过它，不能让整份时间戳都不可用。"""
        audio = tmp_path / "d.mp3"
        audio.write_bytes(b"x")
        marks_sidecar_path(audio).write_text(
            json.dumps(
                {
                    "version": MARKS_FORMAT_VERSION,
                    "marks": [
                        {"text": "好的", "start_sec": 0.0, "duration_sec": 1.0},
                        {"text": "坏的", "start_sec": "不是数字", "duration_sec": 1.0},
                    ],
                }
            ),
            encoding="utf-8",
        )
        back = read_marks_sidecar(audio)
        assert len(back) == 1 and back[0].text == "好的"


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


class TestFactory:
    def test_disabled_by_default(self) -> None:
        """缺省不合成：没配 TTS 必须是一条能跑通的路径。"""
        assert build_tts_provider(Settings(env="test")) is None

    @pytest.mark.parametrize("value", ["", "none", "NONE", "off", "disabled"])
    def test_disabled_spellings(self, value: str) -> None:
        assert build_tts_provider(Settings(env="test"), provider=value) is None

    @pytest.mark.parametrize(
        "name,cls",
        [
            ("edge", EdgeTTSProvider),
            ("doubao", DoubaoTTSProvider),
            ("openai", None),  # 见下面的单独断言（避免 import 期就要求装 openai）
            ("fish", FishAudioTTSProvider),
        ],
    )
    def test_each_provider_builds(self, name: str, cls) -> None:
        provider = build_tts_provider(Settings(env="test"), provider=name)
        assert provider is not None
        if cls is not None:
            assert isinstance(provider, cls)

    def test_unknown_provider_raises(self) -> None:
        """配置写错必须立刻报错，而不是悄悄退化成「没有 TTS」。"""
        with pytest.raises(ValueError) as exc:
            build_tts_provider(Settings(env="test"), provider="bogus")
        assert "bogus" in str(exc.value)


# ---------------------------------------------------------------------------
# 不可用时的提示：必须给出「下一步动作」
# ---------------------------------------------------------------------------


class TestAvailabilityMessages:
    def test_missing_credentials_say_what_to_configure(self) -> None:
        doubao = DoubaoTTSProvider()
        ok, reason = doubao.available()
        assert ok is False and "SCID_DOUBAO_APPID" in reason

        fish = FishAudioTTSProvider()
        ok, reason = fish.available()
        assert ok is False and "SCID_FISH_API_KEY" in reason

    def test_unavailable_provider_raises_non_retryable(self, tmp_path: Path) -> None:
        """缺密钥属**不可重试**：把它当可重试会让编排层烧满 attempt。"""
        provider = FishAudioTTSProvider(api_key="")
        with pytest.raises(TTSError) as exc:
            provider.synthesize("你好", out_path=tmp_path / "x.mp3")
        assert exc.value.retryable is False


# ---------------------------------------------------------------------------
# 豆包（无凭据 / 未验证：只测请求形状与错误分类）
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: object = None, content: bytes = b"") -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class TestDoubaoRequestShape:
    def _provider(self, **kw) -> DoubaoTTSProvider:
        return DoubaoTTSProvider(appid="app", access_token="tok", **kw)

    def test_auth_header_uses_semicolon_form(self, tmp_path: Path, monkeypatch) -> None:
        """火山引擎的鉴权头是 `Bearer;<token>`（分号）。写成空格会鉴权失败，
        而报错通常不会点明这一点 —— 所以这里把它钉死。"""
        captured: dict = {}
        audio = base64.b64encode(b"fake-mp3").decode()

        def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002
            captured.update(url=url, json=json, headers=headers)
            return _FakeResponse(200, {"code": SUCCESS_CODE, "data": audio})

        monkeypatch.setattr("scidirector_ai.tts.doubao.httpx.post", fake_post)
        res = self._provider().synthesize("你好世界", out_path=tmp_path / "d.mp3")

        assert captured["headers"]["Authorization"] == "Bearer;tok"
        assert res.audio_path.read_bytes() == b"fake-mp3"

    def test_payload_has_required_sections(self, tmp_path: Path, monkeypatch) -> None:
        captured: dict = {}
        audio = base64.b64encode(b"x").decode()
        monkeypatch.setattr(
            "scidirector_ai.tts.doubao.httpx.post",
            lambda url, json=None, headers=None, timeout=None: (
                captured.update(json=json) or _FakeResponse(200, {"code": SUCCESS_CODE, "data": audio})
            ),
        )
        self._provider().synthesize("文本", out_path=tmp_path / "d.mp3", speed=1.2)

        body = captured["json"]
        assert set(body) >= {"app", "user", "audio", "request"}
        assert body["app"]["appid"] == "app"
        assert body["audio"]["voice_type"]
        assert body["audio"]["speed_ratio"] == 1.2
        assert body["request"]["text"] == "文本"

    def test_failure_code_is_non_retryable_and_reports_server_message(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "scidirector_ai.tts.doubao.httpx.post",
            lambda *a, **k: _FakeResponse(200, {"code": 3001, "message": "token 无效"}),
        )
        with pytest.raises(TTSError) as exc:
            self._provider().synthesize("x", out_path=tmp_path / "d.mp3")
        assert exc.value.retryable is False
        # 必须把服务端的 code/message 带出来，否则「失败」没法排查
        assert "3001" in str(exc.value) and "token 无效" in str(exc.value)

    def test_server_error_is_retryable(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(
            "scidirector_ai.tts.doubao.httpx.post", lambda *a, **k: _FakeResponse(503, None)
        )
        with pytest.raises(TTSError) as exc:
            self._provider().synthesize("x", out_path=tmp_path / "d.mp3")
        assert exc.value.retryable is True

    def test_non_json_response_is_reported_clearly(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(
            "scidirector_ai.tts.doubao.httpx.post", lambda *a, **k: _FakeResponse(200, None)
        )
        with pytest.raises(TTSError) as exc:
            self._provider().synthesize("x", out_path=tmp_path / "d.mp3")
        assert "JSON" in str(exc.value)

    def test_oversized_text_is_non_retryable(self, tmp_path: Path) -> None:
        with pytest.raises(TTSError) as exc:
            self._provider().synthesize("字" * 5000, out_path=tmp_path / "d.mp3")
        assert exc.value.retryable is False


# ---------------------------------------------------------------------------
# Fish Audio（规范来自其 OpenAPI；端点不通，未端到端验证）
# ---------------------------------------------------------------------------


class TestFishRequestShape:
    def test_auth_and_model_header(self, tmp_path: Path, monkeypatch) -> None:
        captured: dict = {}

        def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002
            captured.update(json=json, headers=headers)
            return _FakeResponse(200, None, content=b"ID3-fake-mp3")

        monkeypatch.setattr("scidirector_ai.tts.fish.httpx.post", fake_post)
        res = FishAudioTTSProvider(api_key="k", model="s2-pro", reference_id="ref1").synthesize(
            "你好", out_path=tmp_path / "f.mp3"
        )

        assert captured["headers"]["Authorization"] == "Bearer k"
        assert captured["headers"]["model"] == "s2-pro"
        assert captured["json"]["reference_id"] == "ref1"
        assert captured["json"]["text"] == "你好"
        assert res.audio_path.read_bytes() == b"ID3-fake-mp3"

    def test_401_is_non_retryable(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(
            "scidirector_ai.tts.fish.httpx.post", lambda *a, **k: _FakeResponse(401, None)
        )
        with pytest.raises(TTSError) as exc:
            FishAudioTTSProvider(api_key="bad").synthesize("x", out_path=tmp_path / "f.mp3")
        assert exc.value.retryable is False

    def test_429_is_retryable(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(
            "scidirector_ai.tts.fish.httpx.post", lambda *a, **k: _FakeResponse(429, None)
        )
        with pytest.raises(TTSError) as exc:
            FishAudioTTSProvider(api_key="k").synthesize("x", out_path=tmp_path / "f.mp3")
        assert exc.value.retryable is True

    def test_json_instead_of_audio_is_rejected(self, tmp_path: Path, monkeypatch) -> None:
        """把 JSON 错误体当成音频落盘，会产出「有内容但播放器打不开」的文件 ——
        比直接报错难查得多，因此必须拦下。"""
        monkeypatch.setattr(
            "scidirector_ai.tts.fish.httpx.post",
            lambda *a, **k: _FakeResponse(200, None, content=b'{"error":"bad model"}'),
        )
        with pytest.raises(TTSError) as exc:
            FishAudioTTSProvider(api_key="k").synthesize("x", out_path=tmp_path / "f.mp3")
        assert "JSON" in str(exc.value)
        assert not (tmp_path / "f.mp3").exists()


# ---------------------------------------------------------------------------
# Edge TTS：本机可真实跑的一家（联网时执行，断网自动跳过）
# ---------------------------------------------------------------------------


class TestEdgeRealSynthesis:
    def test_real_synthesis_returns_sentence_marks(self, tmp_path: Path) -> None:
        """实测钉住两件事：①真的能出音频；②真的有**句级**时间戳。

        第二点是这条适配器最大的价值 —— 有它字幕就能按真实句子起止对齐，
        而不是按文本长度估算。服务端行为变化时这条会红，那是我们想要的信号。
        """
        provider = EdgeTTSProvider()
        ok, reason = provider.available()
        if not ok:
            pytest.skip(f"Edge TTS 不可用: {reason}")

        out = tmp_path / "shot.mp3"
        try:
            res = provider.synthesize(
                "第一句讲勾股定理。第二句给出证明思路。第三句说明它的用途。", out_path=out
            )
        except TTSError as err:
            if err.retryable:
                pytest.skip(f"Edge TTS 网络不可达: {err}")
            raise

        assert res.audio_path.is_file() and res.audio_path.stat().st_size > 1000
        assert res.provider == "edge"
        assert res.duration_sec > 0

        # 句级时间戳：三句话应当至少有 2 条 mark（服务端偶尔合并句子，
        # 因此不苛求恰好 3 条，但「一条都没有」说明能力丢了）
        assert len(res.marks) >= 2, f"未拿到句级时间戳，实际 {res.marks}"
        for mark in res.marks:
            assert mark.start_sec >= 0 and mark.duration_sec > 0
            assert mark.end_sec <= res.duration_sec + 0.5, "时间戳超出音频时长"

        # 时间戳要能落盘并回读（Go 侧据此对齐字幕）
        write_marks_sidecar(out, res)
        assert len(read_marks_sidecar(out)) == len(res.marks)

    def test_empty_text_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(TTSError) as exc:
            EdgeTTSProvider().synthesize("   ", out_path=tmp_path / "x.mp3")
        assert exc.value.retryable is False


class TestOutputPathIsAbsolute:
    """回归：产出的音频路径必须是**绝对路径**。

    踩过的坑：ai 服务跑在 `ai/`、Go worker 跑在 `backend/`，
    生产者给出的相对路径到了消费者那边就是「文件不存在」——
    而 worker 只降级不报错（成片照出、只是没有声音），极难归因。
    本项目 §9 记过同源的坑（子进程路径必须 resolve），这里是跨进程的变体。
    """

    def test_relative_out_path_becomes_absolute(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        provider = FishAudioTTSProvider(api_key="k")
        monkeypatch.setattr(
            "scidirector_ai.tts.fish.httpx.post",
            lambda *a, **k: _FakeResponse(200, None, content=b"ID3-audio"),
        )

        res = provider.synthesize("你好", out_path=Path("relative/shot0.mp3"))

        assert res.audio_path.is_absolute(), f"音频路径必须是绝对路径，实际 {res.audio_path}"
        assert res.audio_path.is_file()

    def test_sidecar_path_is_absolute_too(self, tmp_path: Path, monkeypatch) -> None:
        """sidecar 跟着音频走：音频绝对了，sidecar 也必须绝对，否则 Go 同样读不到。"""
        monkeypatch.chdir(tmp_path)
        provider = FishAudioTTSProvider(api_key="k")
        monkeypatch.setattr(
            "scidirector_ai.tts.fish.httpx.post",
            lambda *a, **k: _FakeResponse(200, None, content=b"ID3-audio"),
        )
        res = provider.synthesize("你好", out_path=Path("rel/x.mp3"))
        side = write_marks_sidecar(res.audio_path, res)
        assert side.is_absolute() and side.is_file()
