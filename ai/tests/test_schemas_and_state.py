"""领域模型与状态定义的单元测试。

重点覆盖那些「模型一定会做错、而程序必须兜住」的路径：
时长约束、标签归一化、可执行反馈的强制要求。
"""

from __future__ import annotations

import re

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
    STYLE_PRESETS,
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
            # 动效档：产品演示、界面讲解、图标/角色动画都归它。
            # 少了这些别名，模型写"界面"/"动效"会被兜底成 AMBIENCE，
            # 而 AMBIENCE 只能画渐变卡 —— 那正是成片"只剩标题卡"的成因。
            ("MOTION", SceneTag.MOTION),
            ("motion", SceneTag.MOTION),
            ("[动效]", SceneTag.MOTION),
            ("界面", SceneTag.MOTION),
            ("图标", SceneTag.MOTION),
            ("[UI]", SceneTag.MOTION),
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
            # 环境镜头现在也走 HTML 动画（不再用固定渐变）；
            # `stock` 只在浏览器不可用时由图节点降级使用。
            (SceneTag.AMBIENCE, RenderEngine.MOTION),
            (SceneTag.MOTION, RenderEngine.MOTION),
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


class TestStylePreset:
    """风格预设：生成期的配色一律由它展开，后期的调色是 Go 侧的 grade。"""

    def test_default_preset_keeps_historical_colors(self) -> None:
        """不填任何东西时必须还是原来那套配色 —— 预设不能悄悄改变既有行为。"""
        guide = StyleGuide()
        assert guide.preset == "default"
        assert guide.primary_color == "#4F8CFF"
        assert guide.background_color == "#0B1020"

    def test_preset_expands_to_palette(self) -> None:
        guide = StyleGuide(preset="tech")
        assert guide.primary_color == STYLE_PRESETS["tech"]["primary_color"]
        assert guide.background_color == STYLE_PRESETS["tech"]["background_color"]

    def test_explicit_colour_wins_over_preset(self) -> None:
        """显式色值优先：预设只是省事的默认，不该覆盖用户明确的要求。"""
        guide = StyleGuide(preset="tech", primary_color="#ABCDEF")
        assert guide.primary_color == "#ABCDEF"
        # 没显式给的那一项仍然来自预设。
        assert guide.background_color == STYLE_PRESETS["tech"]["background_color"]

    def test_unknown_preset_is_rejected_with_the_list(self) -> None:
        """未登记的预设必须报错，不静默回落。

        静默回落的表现是"用户选了暖色调、成片却是蓝色"，且没有任何提示 ——
        与调色方案那边采取的态度一致。
        """
        with pytest.raises(ValidationError) as exc:
            StyleGuide(preset="nope")
        assert "未知的风格预设" in str(exc.value)
        assert "tech" in str(exc.value), "错误信息应当列出可选值"

    def test_every_preset_has_valid_hex_colours(self) -> None:
        """每个预设的两个色值都必须是合法十六进制。

        这条防的是手滑：写错一个色值不会报错，而是被原样注入提示词，
        模型照着画出来的颜色不可控 —— 那种问题极难从成片反推回来。
        """
        hex_re = re.compile(r"^#[0-9A-Fa-f]{6}$")
        for name, palette in STYLE_PRESETS.items():
            for key in ("primary_color", "background_color"):
                value = palette.get(key, "")
                assert hex_re.match(value), f"预设 {name} 的 {key}={value!r} 不是合法色值"
            # 主色与背景色相同会让画面糊成一片，这一条几乎总是配置错误。
            assert palette["primary_color"].lower() != palette["background_color"].lower(), (
                f"预设 {name} 的主色与背景色相同"
            )
