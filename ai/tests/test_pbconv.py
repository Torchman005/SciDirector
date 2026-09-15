"""proto <-> 领域模型转换的测试。

转换逻辑是纯函数，因此可以逐字段验证 —— 枚举拼错、字段漏拷这类 bug
在跨语言边界上极难排查（表现为"Go 侧收到的字段全是默认值"），
必须用测试钉死。

其中 **``shots_payload_json`` 的格式**是最关键的一组：它是 Python -> Go 的
实际数据通道，Go 侧用 ``encoding/json`` 反序列化到 ``pb.ShotSpec``，
因此必须使用 **snake_case 字段名 + 数值枚举**。
用 ``model_dump()`` 得到的 ``"MATH"`` 字符串在 Go 侧是解不出来的。
Go 侧有一个对称的测试（``processor_test.go``）用同一份样例数据验证，
两侧一起构成跨语言契约的回归网。
"""

from __future__ import annotations

import json

import pytest

from scidirector_ai import pbconv
from scidirector_ai.schemas import (
    CriticFeedback,
    FeedbackSource,
    RenderArtifact,
    SceneTag,
    ShotSpec,
    StyleGuide,
)

from scidirector.v1 import common_pb2 as common


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class TestEnumMapping:
    def test_tag_round_trip(self) -> None:
        for tag in SceneTag:
            assert pbconv.tag_from_pb(pbconv.tag_to_pb(tag)) is tag

    def test_engine_round_trip(self) -> None:
        for engine in ("manim", "d3", "echarts", "code_anim", "stock"):
            assert pbconv.engine_from_pb(pbconv.engine_to_pb(engine)) == engine

    def test_unspecified_tag_falls_back_to_ambience(self) -> None:
        """空标签退化为氛围镜头，而不是让整条流水线因为一个空字段停摆。"""
        assert pbconv.tag_from_pb(common.SCENE_TAG_UNSPECIFIED) is SceneTag.AMBIENCE

    def test_unspecified_engine_becomes_empty(self) -> None:
        """必须返回空串而不是某个具体引擎 —— 让上层能识别"未设置"。"""
        assert pbconv.engine_from_pb(common.RENDER_ENGINE_UNSPECIFIED) == ""

    def test_unspecified_status_becomes_empty(self) -> None:
        """任务级事件不带状态；返回 "" 才能让上层用 ``if status`` 判断。"""
        assert pbconv.status_from_pb(common.SHOT_STATUS_UNSPECIFIED) == ""

    def test_status_round_trip(self) -> None:
        for status in (
            "PENDING", "GENERATING", "RENDERING", "CRITIQUING",
            "APPROVED", "REJECTED", "RETRYING", "FAILED", "AWAITING_HUMAN",
        ):
            assert pbconv.status_from_pb(pbconv.status_to_pb(status)) == status

    def test_unknown_status_is_unspecified(self) -> None:
        """未知状态降级为 UNSPECIFIED，而不是让事件构造抛错。"""
        assert pbconv.status_to_pb("NOT_A_STATUS") == common.SHOT_STATUS_UNSPECIFIED
        assert pbconv.status_to_pb("") == common.SHOT_STATUS_UNSPECIFIED


# ---------------------------------------------------------------------------
# 实体
# ---------------------------------------------------------------------------


class TestShotConversion:
    def test_round_trip_preserves_fields(self) -> None:
        shot = ShotSpec(
            shot_id="job-x-s001", index=1, narration="画外音", visual_brief="画面",
            tag=SceneTag.MATH, duration_sec=8.5, keywords=["a", "b"],
            code="print(1)", language="python", meta={"k": "v"},
        )
        back = pbconv.shot_from_pb(pbconv.shot_to_pb(shot))

        assert back.shot_id == shot.shot_id
        assert back.index == shot.index
        assert back.narration == shot.narration
        assert back.visual_brief == shot.visual_brief
        assert back.tag is shot.tag
        assert back.engine is shot.engine
        assert back.duration_sec == shot.duration_sec
        assert back.keywords == shot.keywords
        assert back.code == shot.code
        assert back.language == shot.language
        assert back.meta == shot.meta

    def test_unspecified_engine_is_rederived_from_tag(self) -> None:
        """**关键**：proto 里 engine 为空时，必须按标签重新推导。

        否则"引擎由标签确定性映射"这条铁律会被一个空字段破坏 ——
        Go 侧发来的空引擎会让镜头落到错误的渲染器上。
        """
        pb_shot = common.ShotSpec(shot_id="s", index=0, tag=common.SCENE_TAG_MATH,
                                  duration_sec=5.0)
        # 显式指定 engine 为 UNSPECIFIED（值 0）
        pb_shot.engine = common.RENDER_ENGINE_UNSPECIFIED

        shot = pbconv.shot_from_pb(pb_shot)
        assert shot.engine is not None
        assert shot.engine.value == "manim"

    def test_zero_duration_gets_a_safe_default(self) -> None:
        pb_shot = common.ShotSpec(shot_id="s", index=0, duration_sec=0.0)
        assert pbconv.shot_from_pb(pb_shot).duration_sec > 0


class TestFeedbackConversion:
    def test_round_trip(self) -> None:
        feedback = CriticFeedback(
            passed=False, score=0.42, issues=["字号过小"],
            suggestions=["把字号从 24 提到 48"], model="gpt-4o",
            source=FeedbackSource.VLM, attempt=2,
            logic_score=0.8, readability_score=0.3,
            pacing_score=0.6, aesthetics_score=0.7,
        )
        back = pbconv.feedback_from_pb(pbconv.feedback_to_pb(feedback))

        assert back.passed is False
        assert back.score == pytest.approx(0.42)
        assert back.issues == ["字号过小"]
        assert back.suggestions == ["把字号从 24 提到 48"]
        assert back.source is FeedbackSource.VLM
        assert back.attempt == 2
        assert back.readability_score == pytest.approx(0.3)

    def test_missing_suggestions_are_filled(self) -> None:
        """proto 侧允许不通过却没有建议（人类意见可能只给 issues），
        而领域模型强制要求有建议 —— 转换时必须补齐，而不是抛异常打断链路。"""
        pb_feedback = common.CriticFeedback(passed=False, score=0.3, issues=["太乱"])
        back = pbconv.feedback_from_pb(pb_feedback)
        assert back.suggestions, "没有补齐建议，Pydantic 校验会失败"

    def test_none_feedback_means_passed(self) -> None:
        """没有审查意见时按"通过"处理：它表示这个环节没有否决。"""
        assert pbconv.feedback_from_pb(None).passed is True

    def test_human_source_is_preserved(self) -> None:
        pb_feedback = common.CriticFeedback(
            passed=False, score=0.0, suggestions=["s"],
            source=common.FEEDBACK_SOURCE_HUMAN,
        )
        assert pbconv.feedback_from_pb(pb_feedback).source is FeedbackSource.HUMAN


class TestArtifactConversion:
    def test_round_trip(self) -> None:
        artifact = RenderArtifact(
            artifact_id="a1", shot_id="s1", video_path="/tmp/x.mp4",
            duration_sec=8.0, width=1920, height=1080, fps=30, attempt=2,
            engine="manim", frame_samples=["/tmp/f1.png"], render_cost_sec=12.5,
        )
        back = pbconv.artifact_from_pb(pbconv.artifact_to_pb(artifact))

        assert back.video_path == "/tmp/x.mp4"
        assert back.width == 1920 and back.height == 1080 and back.fps == 30
        assert back.frame_samples == ["/tmp/f1.png"]
        assert back.render_cost_sec == pytest.approx(12.5)

    def test_none_artifact_is_safe(self) -> None:
        assert pbconv.artifact_to_pb(None) is None
        assert pbconv.artifact_from_pb(None).video_path == ""


class TestStyleGuideFromJSON:
    def test_parses_valid_json(self) -> None:
        guide = pbconv.style_guide_from_json('{"min_font_size": 48, "theme": "light"}')
        assert guide.min_font_size == 48
        assert guide.theme == "light"

    @pytest.mark.parametrize(
        "raw",
        ["", "   ", "{ 不是 JSON", "[]", '"字符串"', '{"min_font_size": 99999}'],
    )
    def test_degrades_to_default_instead_of_failing(self, raw: str) -> None:
        """风格是锦上添花，不该因为一个坏 JSON 把整次生成打掉。"""
        assert isinstance(pbconv.style_guide_from_json(raw), StyleGuide)


# ---------------------------------------------------------------------------
# 跨语言数据通道（最关键）
# ---------------------------------------------------------------------------


#: Go 侧 ``processor_test.go`` 用**同一份样例**做反向验证。
#: 两侧任何一边改了字段名或枚举编码，都会有一侧失败。
CROSS_LANGUAGE_SAMPLE_SHOTS = [
    ShotSpec(shot_id="job-x-s000", index=0, narration="开场旁白",
             visual_brief="标题淡入", tag=SceneTag.AMBIENCE, duration_sec=4.5,
             keywords=["开场"]),
    ShotSpec(shot_id="job-x-s001", index=1, narration="公式推导",
             visual_brief="居中展示公式", tag=SceneTag.MATH, duration_sec=12.0,
             keywords=["公式", "推导"]),
]


class TestShotsPayloadJSON:
    """``payload_json`` 是 Python -> Go 的实际数据通道，格式错了两侧就断链。"""

    def _payload(self) -> dict:
        raw = pbconv.shots_payload_json(CROSS_LANGUAGE_SAMPLE_SHOTS, outline="大纲")
        return json.loads(raw)

    def test_top_level_shape(self) -> None:
        payload = self._payload()
        assert set(payload) == {"outline", "shots"}
        assert payload["outline"] == "大纲"
        assert len(payload["shots"]) == len(CROSS_LANGUAGE_SAMPLE_SHOTS)

    def test_uses_snake_case_keys(self) -> None:
        """Go 侧用 ``encoding/json`` 反序列化到 pb.ShotSpec，
        其 json tag 是 proto 的 snake_case 字段名。"""
        shot = self._payload()["shots"][0]
        expected = {"shot_id", "index", "narration", "visual_brief",
                    "tag", "engine", "duration_sec", "keywords"}
        assert set(shot) == expected, f"字段名不符合跨语言约定：{sorted(shot)}"

    def test_enums_are_numeric(self) -> None:
        """**最关键的一条**：枚举必须是数字。

        Go 的枚举字段是 int32；发字符串（"MATH"）会直接反序列化失败，
        整批分镜会变成空表，表现为"导演跑完了但一个镜头都没有"。
        """
        shots = self._payload()["shots"]
        assert isinstance(shots[0]["tag"], int)
        assert isinstance(shots[0]["engine"], int)
        assert shots[0]["tag"] == common.SCENE_TAG_AMBIENCE
        assert shots[1]["tag"] == common.SCENE_TAG_MATH
        assert shots[1]["engine"] == common.RENDER_ENGINE_MANIM

    def test_no_extra_fields(self) -> None:
        """不要夹带 Go 侧未声明的字段 —— 那会被静默忽略，
        让人误以为"同步过了"，实际上没有。"""
        for shot in self._payload()["shots"]:
            assert "code" not in shot
            assert "status" not in shot

    def test_is_valid_json(self) -> None:
        pbconv.shots_payload_json(CROSS_LANGUAGE_SAMPLE_SHOTS, outline="中文大纲")
        # 中文不能被转义成 \uXXXX（Go 侧能解，但日志可读性会变差）
        raw = pbconv.shots_payload_json(CROSS_LANGUAGE_SAMPLE_SHOTS, outline="中文大纲")
        assert "中文大纲" in raw


class TestEventToPB:
    def test_maps_core_fields(self) -> None:
        event = {
            "job_id": "j1", "shot_id": "s1", "node": "render", "status": "RENDERING",
            "message": "渲染完成", "attempt": 2, "shot_index": 1, "total_shots": 3,
            "progress": 0.5, "error": "", "ts_unix_ms": 1_700_000_000_000,
            "payload_json": "{}",
        }
        pb_event = pbconv.event_to_pb(event)

        assert pb_event.job_id == "j1"
        assert pb_event.shot_id == "s1"
        assert pb_event.node == "render"
        assert pb_event.status == common.SHOT_STATUS_RENDERING
        assert pb_event.attempt == 2
        assert pb_event.progress == pytest.approx(0.5)
        assert pb_event.ts_unix_ms == 1_700_000_000_000

    def test_nested_artifact_and_feedback(self) -> None:
        event = {
            "job_id": "j1", "node": "critique",
            "artifact": RenderArtifact(shot_id="s1", video_path="/tmp/x.mp4", width=1920),
            "feedback": CriticFeedback(passed=True, score=0.9),
        }
        pb_event = pbconv.event_to_pb(event)
        assert pb_event.artifact.video_path == "/tmp/x.mp4"
        assert pb_event.artifact.width == 1920
        assert pb_event.feedback.passed is True

    def test_missing_ts_gets_a_default(self) -> None:
        """缺时间戳时必须补上，否则 Go 侧会用"当前时间"兜底，
        在断点续跑场景下产生"时间倒流"的假象。"""
        pb_event = pbconv.event_to_pb({"job_id": "j1", "node": "plan"})
        assert pb_event.ts_unix_ms > 0

    def test_empty_event_is_safe(self) -> None:
        pb_event = pbconv.event_to_pb({})
        assert pb_event.status == common.SHOT_STATUS_UNSPECIFIED
        # 注意 proto3 的语义：**未设置的 message 字段读出来是空消息，不是 None**。
        # 因此判断"有没有"必须用 HasField，而不是 `is None`。
        assert not pb_event.HasField("artifact")
        assert not pb_event.HasField("feedback")
