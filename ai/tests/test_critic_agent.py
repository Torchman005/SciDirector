"""``CriticAgent`` 的单元测试。

重点覆盖"模型的判断不可全信、但也不能不信"这条核心逻辑，以及两条兜底路径：

* 模型给了**不可执行**的建议 -> 从 issues 派生可执行指令（还能自动修）；
* 连 issues 都不可用 -> **不伪造建议**，转人工（而不是硬判通过或硬判失败）。

这三条决定了重试回路是"收敛"还是"空转烧钱"，因此断言写得比较死。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scidirector_ai.agents.critic import (
    DIMENSION_FLOORS,
    DIMENSION_WEIGHTS,
    CriticAgent,
    _RawCritique,
    _derive_suggestions,
    _sanitize_suggestions,
    verdict_mismatch,
)
from scidirector_ai.config import Settings
from scidirector_ai.llm import LLMClient
from scidirector_ai.schemas import (
    FeedbackSource,
    RenderArtifact,
    SceneTag,
    ShotSpec,
    StyleGuide,
)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def make_settings(**overrides: object) -> Settings:
    return Settings(env="test", llm_provider="mock", **overrides)  # type: ignore[arg-type]


@pytest.fixture()
def agent() -> CriticAgent:
    return CriticAgent(LLMClient(make_settings()), make_settings())


@pytest.fixture()
def shot() -> ShotSpec:
    return ShotSpec(
        shot_id="job-x-s001",
        index=1,
        narration="把等号两侧同时平方，得到最终形式。",
        visual_brief="居中展示公式，逐项高亮。",
        tag=SceneTag.MATH,
        duration_sec=8.0,
    )


def make_artifact(tmp_path: Path, frames: int = 3, **overrides: object) -> RenderArtifact:
    """造一个带**真实存在**的抽帧文件的产物。

    审查会先过滤掉不存在的文件；用假路径会让测试走进"无帧可用"的降级分支，
    从而测不到正常路径。
    """
    paths: list[str] = []
    for i in range(frames):
        frame = tmp_path / f"frame_{i:02d}.png"
        frame.write_bytes(b"\x89PNG\r\n\x1a\n")  # 只要求文件存在
        paths.append(str(frame))
    payload: dict[str, object] = {
        "artifact_id": "a1",
        "shot_id": "job-x-s001",
        "video_path": str(tmp_path / "shot.mp4"),
        "duration_sec": 8.1,
        "width": 1920,
        "height": 1080,
        "fps": 30,
        "attempt": 1,
        "engine": "manim",
        "frame_samples": paths,
    }
    payload.update(overrides)
    return RenderArtifact(**payload)  # type: ignore[arg-type]


def raw(**overrides: object) -> _RawCritique:
    """构造一份模型输出，默认是"各方面都不错且自称通过"。"""
    base: dict[str, object] = {
        "passed": True,
        "score": 0.9,
        "logic_score": 0.9,
        "readability_score": 0.85,
        "pacing_score": 0.8,
        "aesthetics_score": 0.85,
        "issues": [],
        "suggestions": [],
    }
    base.update(overrides)
    return _RawCritique(**base)  # type: ignore[arg-type]


# ===========================================================================
# 判定逻辑：阈值与维度下限
# ===========================================================================


class TestDecisionRules:
    def test_good_shot_passes_without_suggestions(self, agent: CriticAgent, shot: ShotSpec) -> None:
        feedback, program_passed = agent._decide(raw(), shot=shot, attempt=1)
        assert program_passed is True
        assert feedback.passed is True
        assert feedback.suggestions == [], "通过时不得给出建议，否则下游会无限重做"

    def test_score_is_computed_by_weights_not_taken_from_model(
        self, agent: CriticAgent, shot: ShotSpec
    ) -> None:
        """总分必须由程序按权重算。

        模型自报 score=1.0 但四个维度都是 0.5 —— 若采信模型的自报分，
        一个平庸的画面就会被放行。
        """
        feedback, _ = agent._decide(
            raw(score=1.0, logic_score=0.5, readability_score=0.5,
                pacing_score=0.5, aesthetics_score=0.5),
            shot=shot,
            attempt=1,
        )
        assert feedback.score == pytest.approx(0.5, abs=1e-6)

    def test_weighted_score_matches_prompt_formula(
        self, agent: CriticAgent, shot: ShotSpec
    ) -> None:
        """算分公式必须与 prompts/critic.md 里写的一致。"""
        feedback, _ = agent._decide(
            raw(logic_score=1.0, readability_score=0.0, pacing_score=0.0, aesthetics_score=0.0),
            shot=shot,
            attempt=1,
        )
        assert feedback.score == pytest.approx(DIMENSION_WEIGHTS["logic_score"], abs=1e-6)

    def test_below_threshold_fails(self, agent: CriticAgent, shot: ShotSpec) -> None:
        feedback, program_passed = agent._decide(
            raw(logic_score=0.6, readability_score=0.5, pacing_score=0.4, aesthetics_score=0.4),
            shot=shot,
            attempt=1,
        )
        assert program_passed is False
        assert feedback.passed is False
        assert feedback.suggestions, "不通过必须给出可执行建议"

    def test_dimension_floor_blocks_high_total(
        self, agent: CriticAgent, shot: ShotSpec
    ) -> None:
        """**关键**：某一维度塌陷时，高总分也不能放行。

        理由：总分是加权平均，一个略低于下限的可读性会被其他三项拉到阈值以上，
        但"看不清"的画面没有任何交付价值。

        用例刻意取 readability 略低于下限（0.59 < 0.60）而总分 0.88 ——
        这样"拦住它的"确实只有维度下限，而不是总分阈值。
        """
        feedback, program_passed = agent._decide(
            raw(logic_score=1.0, readability_score=0.59, pacing_score=1.0, aesthetics_score=1.0),
            shot=shot,
            attempt=1,
        )
        assert feedback.score > 0.75, "构造用例失败：总分本应高于阈值，否则测不到下限的作用"
        assert program_passed is False, "可读性未达下限却通过了"
        assert any("文字可读性" in i for i in feedback.issues)

    def test_floor_is_what_blocks_not_the_threshold(
        self, agent: CriticAgent, shot: ShotSpec
    ) -> None:
        """反向对照：同样低的可读性，若**不**设下限就不会被拦。

        这条断言把"下限"与"阈值"两个机制区分开 ——
        否则上面那条测试可能只是被总分阈值拦下的，等于没测到下限。
        """
        below = dict(logic_score=1.0, readability_score=0.59, pacing_score=1.0, aesthetics_score=1.0)
        _, passed_with_floor = agent._decide(raw(**below), shot=shot, attempt=1)
        assert passed_with_floor is False

        # 直接把可读性抬到下限之上，其余不变 -> 立刻通过。
        # 说明拦住它的确实是下限，而不是其它因素。
        above = dict(below, readability_score=0.60)
        _, passed_above_floor = agent._decide(raw(**above), shot=shot, attempt=1)
        assert passed_above_floor is True, "抬到下限之上仍未通过，说明拦住它的另有其因"

    @pytest.mark.parametrize("dim", list(DIMENSION_FLOORS))
    def test_each_floor_is_enforced(self, agent: CriticAgent, shot: ShotSpec, dim: str) -> None:
        """每个设了下限的维度都要真的被检查到（防止有人加了常量却忘了用）。"""
        values = {"logic_score": 1.0, "readability_score": 1.0,
                  "pacing_score": 1.0, "aesthetics_score": 1.0}
        values[dim] = DIMENSION_FLOORS[dim] - 0.01
        _, program_passed = agent._decide(raw(**values), shot=shot, attempt=1)
        assert program_passed is False, f"{dim} 未达下限却通过了"

    def test_scores_are_clamped(self, agent: CriticAgent, shot: ShotSpec) -> None:
        """模型给出越界分（负数 / >1）时必须夹住，不能让脏数据影响判定。"""
        feedback, _ = agent._decide(
            raw(logic_score=9.9, readability_score=-3.0, pacing_score=0.8, aesthetics_score=0.8),
            shot=shot,
            attempt=1,
        )
        assert 0.0 <= feedback.readability_score <= 1.0
        assert 0.0 <= feedback.score <= 1.0


# ===========================================================================
# 模型与程序的分歧
# ===========================================================================


class TestVerdictCombination:
    def test_model_veto_is_honoured(self, agent: CriticAgent, shot: ShotSpec) -> None:
        """模型说不通过就必须不通过，即使程序算出来分数很高。

        模型可能看到程序看不到的致命问题（画面全黑、乱码方块）。
        """
        feedback, program_passed = agent._decide(
            raw(passed=False, logic_score=0.95, readability_score=0.95,
                pacing_score=0.95, aesthetics_score=0.95,
                issues=["画面出现乱码方块（字体缺失）"],
                suggestions=["检查中文字体是否安装"]),
            shot=shot,
            attempt=1,
        )
        assert program_passed is True, "程序侧本应判为通过"
        assert feedback.passed is False, "模型的否决票没有被采信"
        assert feedback.suggestions

    def test_model_pass_cannot_override_program_failure(
        self, agent: CriticAgent, shot: ShotSpec
    ) -> None:
        """模型自称通过不算数 —— 否则 0.74 会被它说成通过。"""
        feedback, _ = agent._decide(
            raw(passed=True, logic_score=0.5, readability_score=0.5,
                pacing_score=0.5, aesthetics_score=0.5),
            shot=shot,
            attempt=1,
        )
        assert feedback.passed is False
        assert any("模型自报通过" in i for i in feedback.issues), (
            "分歧必须写进 issues，否则运维看不出提示词没被正确执行"
        )

    def test_verdict_mismatch_helper(self) -> None:
        assert verdict_mismatch(True, False) is True
        assert verdict_mismatch(False, True) is False
        assert verdict_mismatch(True, True) is False
        assert verdict_mismatch(False, False) is False


# ===========================================================================
# 建议的可用性兜底
# ===========================================================================


class TestSuggestionHandling:
    def test_vague_suggestions_are_dropped(self) -> None:
        kept = _sanitize_suggestions(["画面不好看", "太丑了", "感觉不太行"])
        assert kept == []

    def test_actionable_suggestions_are_kept(self) -> None:
        kept = _sanitize_suggestions(
            ["把字号从 24 提到 48", "把 x 轴刻度从 10 个减到 6 个", "放大标题"]
        )
        assert len(kept) == 3

    def test_suggestions_are_deduplicated_and_capped(self) -> None:
        many = [f"把字号从 {i} 提到 {i + 10}" for i in range(20)]
        kept = _sanitize_suggestions(many + many)
        assert len(kept) == 6, "建议数量必须封顶，否则模型一次列十几条、重写时顾此失彼"

    def test_derives_actionable_from_issues(self, agent: CriticAgent, shot: ShotSpec) -> None:
        """模型只给问题、不给建议时，必须能派生出可执行指令。

        不派生的后果：重试没有方向，必然产出同样的画面，白烧一轮渲染。
        """
        feedback, _ = agent._decide(
            raw(passed=False, logic_score=0.5, readability_score=0.5,
                pacing_score=0.5, aesthetics_score=0.5,
                issues=["第 2 帧坐标轴标签相互重叠"],
                suggestions=[]),
            shot=shot,
            attempt=1,
        )
        assert feedback.suggestions
        assert any("重叠" in s for s in feedback.suggestions)

    @pytest.mark.parametrize(
        ("issue", "expect"),
        [
            ("字号过小无法辨认", "font-size"),
            ("动画推进太快", "run_time"),
            ("公式超出画面", "scale_to_fit_width"),
            ("开始出现乱码方块", "字体"),
            ("画面全黑", "play()"),
        ],
    )
    def test_derivation_covers_common_failure_modes(self, issue: str, expect: str) -> None:
        derived = _derive_suggestions([issue])
        assert derived and expect in derived[0], f"{issue!r} 未派生出含 {expect!r} 的建议"

    def test_falls_back_to_human_guidance_when_nothing_usable(
        self, agent: CriticAgent, shot: ShotSpec
    ) -> None:
        """连 issues 都不可用时**不伪造建议**，而是给出明确的人工介入指引。

        伪造一条看似具体的建议会触发一轮必然失败的重试；
        转人工虽然慢，但至少不会把钱烧在注定失败的尝试上。
        """
        feedback, _ = agent._decide(
            raw(passed=False, logic_score=0.4, readability_score=0.4,
                pacing_score=0.4, aesthetics_score=0.4,
                issues=[], suggestions=[]),
            shot=shot,
            attempt=1,
        )
        assert feedback.passed is False
        assert feedback.suggestions
        assert "人工" in feedback.suggestions[0]
        assert any("不可执行" in i for i in feedback.issues)


# ===========================================================================
# 端到端（走 mock LLM）
# ===========================================================================


class TestReviewEndToEnd:
    def test_reviews_with_mock_llm(
        self, agent: CriticAgent, shot: ShotSpec, tmp_path: Path
    ) -> None:
        outcome = agent.review(
            shot=shot,
            artifact=make_artifact(tmp_path),
            style_guide=StyleGuide(),
            attempt=1,
        )
        assert outcome.degraded is False
        assert outcome.frames_reviewed == 3
        assert outcome.feedback.source is FeedbackSource.VLM
        assert outcome.feedback.model

    def test_no_frames_degrades_instead_of_passing(
        self, agent: CriticAgent, shot: ShotSpec, tmp_path: Path
    ) -> None:
        """没有抽帧时必须降级转人工，**绝不能**盲判通过。

        盲判通过 = 把未审查的画面放进成片，这是本项目最不能接受的失败形态。
        """
        outcome = agent.review(
            shot=shot,
            artifact=make_artifact(tmp_path, frames=0),
            style_guide=StyleGuide(),
            attempt=1,
        )
        assert outcome.degraded is True
        assert outcome.feedback.passed is False
        assert outcome.feedback.source is FeedbackSource.SYSTEM
        assert "抽帧" in outcome.degradation_reason

    def test_missing_frame_files_also_degrade(
        self, agent: CriticAgent, shot: ShotSpec, tmp_path: Path
    ) -> None:
        """抽帧路径存在但文件已被清理 —— 同样必须降级，而不是让调用崩掉。"""
        artifact = make_artifact(tmp_path)
        for path in artifact.frame_samples:
            Path(path).unlink()
        outcome = agent.review(
            shot=shot, artifact=artifact, style_guide=StyleGuide(), attempt=1
        )
        assert outcome.degraded is True

    def test_rejected_mock_produces_actionable_feedback(
        self,
        shot: ShotSpec,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """用 SCID_MOCK_CRITIC_PASSED=false 演练"打回重做"链路。

        这条开关让 HITL 闭环可以在没有真实 VLM 的情况下端到端联调。
        """
        monkeypatch.setenv("SCID_MOCK_CRITIC_PASSED", "false")
        settings = make_settings()
        agent = CriticAgent(LLMClient(settings), settings)

        outcome = agent.review(
            shot=shot, artifact=make_artifact(tmp_path), style_guide=StyleGuide(), attempt=1
        )
        assert outcome.feedback.passed is False
        assert outcome.feedback.suggestions, "打回时必须带上可执行建议"
        assert outcome.feedback.score < 0.75

    def test_llm_failure_degrades(
        self, agent: CriticAgent, shot: ShotSpec, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """VLM 调用失败 -> 降级转人工，而不是抛异常把整条流水线打断。"""
        from scidirector_ai import llm as llm_module

        def boom(*args: object, **kwargs: object) -> object:
            raise llm_module.LLMError("模拟 VLM 不可用")

        monkeypatch.setattr(agent.llm, "vision_json", boom)
        outcome = agent.review(
            shot=shot, artifact=make_artifact(tmp_path), style_guide=StyleGuide(), attempt=1
        )
        assert outcome.degraded is True
        assert outcome.feedback.passed is False
        assert "VLM" in outcome.degradation_reason

    def test_degraded_outcome_never_claims_pass(
        self, agent: CriticAgent, shot: ShotSpec
    ) -> None:
        outcome = agent._degrade(shot=shot, attempt=2, reason="测试原因")
        assert outcome.feedback.passed is False
        assert outcome.degraded is True
        assert outcome.feedback.suggestions
        assert outcome.feedback.source is FeedbackSource.SYSTEM

    def test_previous_feedback_is_injected(
        self, agent: CriticAgent, shot: ShotSpec, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """上一轮反馈必须进入用户提示词，供模型确认是否已修复。

        不注入的话模型会给出一模一样的意见，重试循环永远收敛不了。
        """
        captured: dict[str, str] = {}
        original = agent.llm.vision_json

        def spy(system: str, user: str, schema: type, images: list, **kwargs: object) -> object:
            captured["user"] = user
            return original(system, user, schema, images, **kwargs)

        monkeypatch.setattr(agent.llm, "vision_json", spy)
        agent.review(
            shot=shot,
            artifact=make_artifact(tmp_path),
            style_guide=StyleGuide(),
            attempt=2,
            previous_feedback="字号过小，已要求放大到 48",
        )
        assert "上一轮反馈" in captured["user"]
        assert "字号过小" in captured["user"]

    def test_threshold_is_injected_into_prompt(
        self, shot: ShotSpec, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """阈值必须来自配置而不是写死在提示词里。"""
        settings = make_settings(critic_score_threshold=0.9)
        agent = CriticAgent(LLMClient(settings), settings)
        captured: dict[str, str] = {}
        original = agent.llm.vision_json

        def spy(system: str, user: str, schema: type, images: list, **kwargs: object) -> object:
            captured["system"] = system
            return original(system, user, schema, images, **kwargs)

        monkeypatch.setattr(agent.llm, "vision_json", spy)
        agent.review(
            shot=shot, artifact=make_artifact(tmp_path), style_guide=StyleGuide(), attempt=1
        )
        assert "0.90" in captured["system"]

    def test_prompt_is_valid_json_template(
        self, agent: CriticAgent, shot: ShotSpec, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """发给模型的系统提示词里必须带一个**可解析**的 JSON 示例。

        示例本身不合法 JSON 的话，模型会照抄出一个不合法的输出。
        """
        import re

        captured: dict[str, str] = {}
        original = agent.llm.vision_json

        def spy(system: str, user: str, schema: type, images: list, **kwargs: object) -> object:
            captured["system"] = system
            return original(system, user, schema, images, **kwargs)

        monkeypatch.setattr(agent.llm, "vision_json", spy)
        agent.review(
            shot=shot, artifact=make_artifact(tmp_path), style_guide=StyleGuide(), attempt=1
        )
        block = re.search(r"```json\s*(.*?)```", captured["system"], re.DOTALL)
        assert block, "系统提示词里没有 json 代码块"
        parsed = json.loads(block.group(1))
        assert isinstance(parsed["passed"], bool)
        assert "suggestions" in parsed
