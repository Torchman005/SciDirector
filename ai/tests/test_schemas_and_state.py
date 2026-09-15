"""领域模型与状态定义的单元测试。

重点覆盖那些「模型一定会做错、而程序必须兜住」的路径：
时长约束、标签归一化、可执行反馈的强制要求。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scidirector_ai.graph.state import (
    initial_state,
    current_shot,
    make_event,
    progress_ratio,
    shot_attempt,
)
from scidirector_ai.schemas import (
    TAG_TO_ENGINE,
    CriticFeedback,
    FeedbackSource,
    RenderEngine,
    SceneTag,
    ShotSpec,
    StyleGuide,
)


class TestSceneTag:
    """标签归一化：模型输出千奇百怪，必须能收敛。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("MATH", SceneTag.MATH),
            ("math", SceneTag.MATH),
            ("[数学]", SceneTag.MATH),
            ("【数据】", SceneTag.DATA),
            ("图表", SceneTag.DATA),
            ("code", SceneTag.CODE),
            ("氛围", SceneTag.AMBIENCE),
            ("完全无法识别的内容", SceneTag.AMBIENCE),  # 兜底
            ("", SceneTag.AMBIENCE),
        ],
    )
    def test_from_prompt(self, raw: str, expected: SceneTag) -> None:
        assert SceneTag.from_prompt(raw) is expected


class TestTagRouting:
    """标签 -> 引擎必须是确定性映射（模型无权自由指定引擎）。"""

    def test_mapping_is_complete(self) -> None:
        assert set(TAG_TO_ENGINE) == set(SceneTag)

    @pytest.mark.parametrize(
        ("tag", "engine"),
        [
            (SceneTag.MATH, RenderEngine.MANIM),
            (SceneTag.DATA, RenderEngine.D3),
            (SceneTag.CODE, RenderEngine.CODE_ANIM),
            (SceneTag.AMBIENCE, RenderEngine.STOCK),
        ],
    )
    def test_engine_derived_from_tag(self, tag: SceneTag, engine: RenderEngine) -> None:
        shot = ShotSpec(tag=tag)
        assert shot.engine is engine


class TestShotSpec:
    """分镜的输入校验与看护。"""

    def test_duration_string_is_coerced(self) -> None:
        """模型常输出 "5s" / "5 秒"，必须能解析。"""
        assert ShotSpec(duration_sec="5s").duration_sec == 5.0
        assert ShotSpec(duration_sec="12 秒").duration_sec == 12.0

    def test_keywords_string_is_split(self) -> None:
        shot = ShotSpec(keywords="导数，切线、极限")
        assert shot.keywords == ["导数", "切线", "极限"]

    def test_duration_bounds_enforced(self) -> None:
        with pytest.raises(ValidationError):
            ShotSpec(duration_sec=0)
        with pytest.raises(ValidationError):
            ShotSpec(duration_sec=10_000)


class TestCriticFeedback:
    """反馈的契约级约束：不通过时必须给出可执行建议。"""

    def test_rejects_unactionable_failure(self) -> None:
        """「不合格但不说怎么改」必须在数据层就被拦住。

        否则回灌给编码智能体只会得到同样的画面，白白烧掉一次渲染 + 一次 VLM 调用。
        """
        with pytest.raises(ValidationError):
            CriticFeedback(passed=False, score=0.3, issues=["画面不好看"])

    def test_accepts_actionable_failure(self) -> None:
        feedback = CriticFeedback(
            passed=False,
            score=0.3,
            issues=["字号过小"],
            suggestions=["把字号从 24 提到 48"],
        )
        assert feedback.actionable

    def test_passing_needs_no_suggestions(self) -> None:
        feedback = CriticFeedback(passed=True, score=0.9)
        assert feedback.passed
        assert not feedback.actionable

    def test_score_range(self) -> None:
        with pytest.raises(ValidationError):
            CriticFeedback(passed=True, score=1.5)


class TestStyleGuide:
    def test_resolution_by_aspect_ratio(self) -> None:
        assert StyleGuide(aspect_ratio="16:9").resolution == (1920, 1080)
        assert StyleGuide(aspect_ratio="9:16").resolution == (1080, 1920)


class TestPipelineState:
    """状态构造、游标安全与进度计算。"""

    def _state(self, shot_count: int = 3):
        state = initial_state(
            job_id="job-test",
            raw_script="脚本",
            style_guide=StyleGuide(),
            target_duration_sec=30,
            max_attempts_per_shot=3,
            locale="zh-CN",
        )
        state["shots"] = [ShotSpec(shot_id=f"job-test-s{i:03d}", index=i) for i in range(shot_count)]
        return state

    def test_initial_state_has_containers(self) -> None:
        """容器字段必须被初始化：忘记初始化 attempts 会直接导致 KeyError。"""
        state = self._state()
        assert state["artifacts"] == {}
        assert state["attempts"] == {}
        assert state["feedback"] == {}
        assert state["events"] == []
        assert state["errors"] == []

    def test_current_shot_out_of_range_returns_none(self) -> None:
        """越界必须返回 None 而不是抛 IndexError —— 并发/重试下越界是正常情况。"""
        state = self._state(2)
        state["cursor"] = 0
        assert current_shot(state) is not None
        state["cursor"] = 5
        assert current_shot(state) is None
        state["cursor"] = -1
        assert current_shot(state) is None

    def test_shot_attempt_defaults_to_zero(self) -> None:
        state = self._state()
        assert shot_attempt(state, "job-test-s000") == 0
        state["attempts"]["job-test-s000"] = 2
        assert shot_attempt(state, "job-test-s000") == 2

    def test_progress_counts_only_passed(self) -> None:
        state = self._state(4)
        state["feedback"]["job-test-s000"] = CriticFeedback(passed=True, score=0.9)
        state["feedback"]["job-test-s001"] = CriticFeedback(
            passed=False, score=0.4, issues=["太小"], suggestions=["放大"]
        )
        # 只有 1/4 通过。
        assert progress_ratio(state) == 0.25

    def test_progress_empty_shots(self) -> None:
        state = self._state(0)
        assert progress_ratio(state) == 0.0

    def test_make_event_fills_required_fields(self) -> None:
        """事件必须字段齐全：缺 ts_unix_ms 会让 Go 侧用当前时间兜底，
        在断点续跑时产生「时间倒流」的假象，干扰排查。"""
        state = self._state(2)
        state["cursor"] = 1
        event = make_event(state, node="code", message="开始生成代码", status="GENERATING")
        for key in (
            "job_id",
            "node",
            "status",
            "message",
            "attempt",
            "shot_index",
            "total_shots",
            "progress",
            "ts_unix_ms",
        ):
            assert key in event
        assert event["shot_index"] == 1
        assert event["total_shots"] == 2
        assert event["ts_unix_ms"] > 0


class TestFeedbackSource:
    def test_default_source_is_vlm(self) -> None:
        assert CriticFeedback(passed=True, score=0.9).source is FeedbackSource.VLM
