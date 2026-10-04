"""镜头时长上限与**自动拆分**。

守的是一条真实事故（`job-afdfcd4350073365-s000`）：一条 **22.83 秒**的镜头，
动画演到约 30% 处就静止（相邻抽帧的变化像素占比 `0.19% / 0.12% / 0.06%`），
审查连续四轮判"节奏停滞"——**判断是对的**，重试也救不回来。

根因在导演阶段：提示词写着"单个分镜时长必须在 2.0 ~ 25.0 秒之间"，
还鼓励"核心推导与数据镜头应当更长（10~20 秒）"——
上限太高、方向还在鼓励拉长。**时长越长，越难让画面一直有变化。**

而且这里有个容易被忽略的机制：Go 侧合成会把该镜头的配音**对齐到画面时长**
（不足补静音、超长裁掉）。所以"旁白只有一句话却给了 15 秒"的结果就是
十几秒的静音加静止画面 —— 光靠提示词管不住，必须有程序化兜底。
"""

from __future__ import annotations

import pytest

from scidirector_ai.agents.base import render_prompt
from scidirector_ai.agents.director import (
    MAX_SHOT_SEC,
    MIN_SHOT_SEC,
    DirectorAgent,
    _continuation_brief,
    _group_sentences,
    _split_sentences,
)
from scidirector_ai.llm import LLMClient
from scidirector_ai.config import Settings
from scidirector_ai.schemas import SceneTag, ShotSpec

#: 那条真实镜头的旁白（22.83 秒，一整段话）。拆它就是为了这件事。
LONG_NARRATION = (
    "为什么大模型读长文这么慢？为什么上下文一长，它就失忆了？"
    "因为注意力机制要和前面每一个词算一遍相关性，长度翻倍，计算量的平方就翻倍。"
)


def make_agent() -> DirectorAgent:
    settings = Settings(env="test", llm_provider="mock")
    return DirectorAgent(LLMClient(settings))


def make_shot(**overrides: object) -> ShotSpec:
    base: dict[str, object] = {
        "shot_id": "job-x-s000",
        "index": 0,
        "narration": LONG_NARRATION,
        "visual_brief": "聊天界面：长文本被逐段高亮，进度条卡在 90%。",
        "tag": SceneTag.MOTION,
        "duration_sec": 22.83,
        "keywords": ["长文本", "注意力"],
    }
    base.update(overrides)
    return ShotSpec(**base)  # type: ignore[arg-type]


class TestSentenceSplit:
    def test_splits_on_chinese_punctuation(self) -> None:
        got = _split_sentences("第一句。第二句！第三句？")
        assert got == ["第一句。", "第二句！", "第三句？"]

    def test_keeps_trailing_text_without_punctuation(self) -> None:
        assert _split_sentences("完整的一句。没有标点的尾巴") == [
            "完整的一句。",
            "没有标点的尾巴",
        ]

    def test_never_splits_a_sentence_in_half(self) -> None:
        """半句话会让配音断裂、让该段画面意图没法理解。"""
        got = _split_sentences(LONG_NARRATION)
        assert len(got) == 3
        assert "".join(got) == LONG_NARRATION


class TestSentenceGrouping:
    def test_groups_are_balanced_by_characters(self) -> None:
        sentences = _split_sentences(LONG_NARRATION)
        groups = _group_sentences(sentences, 2)
        assert len(groups) == 2
        # 按字数分组：最长的组不该超过总字数的 3/4（否则等于没分）。
        total = sum(len(s) for s in sentences)
        assert max(len(g) for g in groups) <= total * 0.75

    def test_group_count_never_exceeds_sentence_count(self) -> None:
        """句子不够时宁可少分几组 —— 空组会产出**没有旁白的镜头**。"""
        assert len(_group_sentences(["只有一句话。"], 3)) == 1
        assert len(_group_sentences(_split_sentences(LONG_NARRATION), 6)) == 3

    def test_empty_input(self) -> None:
        assert _group_sentences([], 3) == []

    def test_groups_cover_every_sentence_exactly_once(self) -> None:
        sentences = _split_sentences(LONG_NARRATION)
        for parts in (1, 2, 3):
            assert "".join(_group_sentences(sentences, parts)) == LONG_NARRATION


class TestSplitLongShots:
    def test_long_shot_is_split(self) -> None:
        agent = make_agent()
        got = agent._split_long_shots([make_shot()], job_id="job-x")

        assert len(got) == 2, "22.83 秒超过 15 秒上限，应当拆成 2 段"
        # 时长必须守恒：配音对齐到画面时长，总时长变了会让整片对不上。
        assert abs(sum(s.duration_sec for s in got) - 22.83) < 0.05
        # 每段都不能再超过上限。
        assert all(s.duration_sec <= MAX_SHOT_SEC for s in got)

    def test_durations_follow_narration_length(self) -> None:
        """按字数分配时长，各段的音画才对得上。"""
        agent = make_agent()
        got = agent._split_long_shots([make_shot()], job_id="job-x")
        for shot in got:
            expected = 22.83 * len(shot.narration) / len(LONG_NARRATION)
            assert abs(shot.duration_sec - expected) < 0.6

    def test_indices_and_ids_are_renumbered(self) -> None:
        """下游按 index 定位镜头（写回、事件、界面排序），必须连续。"""
        agent = make_agent()
        got = agent._split_long_shots([make_shot(), make_shot(index=1)], job_id="job-x")
        assert [s.index for s in got] == list(range(len(got)))
        assert [s.shot_id for s in got] == [f"job-x-s{i:03d}" for i in range(len(got))]

    def test_narration_is_preserved_in_order(self) -> None:
        agent = make_agent()
        got = agent._split_long_shots([make_shot()], job_id="job-x")
        assert "".join(s.narration for s in got) == LONG_NARRATION

    def test_short_shot_is_left_alone(self) -> None:
        agent = make_agent()
        original = make_shot(duration_sec=8.0)
        got = agent._split_long_shots([original], job_id="job-x")
        assert got == [original]
        assert got[0] is original, "没超限时不该复制对象"

    def test_silent_shot_splits_duration_evenly(self) -> None:
        """没有旁白（氛围/标题）没有可切的句子，等分时长。"""
        agent = make_agent()
        got = agent._split_long_shots(
            [make_shot(narration="", tag=SceneTag.AMBIENCE)], job_id="job-x"
        )
        assert len(got) == 2
        assert all(not s.narration for s in got)
        assert abs(sum(s.duration_sec for s in got) - 22.83) < 0.05

    def test_single_sentence_shot_is_kept_not_padded(self) -> None:
        """一句话撑二十多秒：不拆（拆就等于重复或清空旁白），但要留下来。"""
        agent = make_agent()
        got = agent._split_long_shots(
            [make_shot(narration="只有一句话。")], job_id="job-x"
        )
        assert len(got) == 1, "不该产出没有旁白的镜头"
        assert got[0].narration == "只有一句话。"

    def test_visual_brief_marks_the_part(self) -> None:
        """不标段号的话，编码端会把同一个画面画 N 遍。"""
        agent = make_agent()
        got = agent._split_long_shots([make_shot()], job_id="job-x")
        assert "第 1 段" in got[0].visual_brief
        assert "最后一段" in got[1].visual_brief

    def test_empty_input(self) -> None:
        assert make_agent()._split_long_shots([], job_id="job-x") == []


class TestContinuationBrief:
    def test_single_part_is_unchanged(self) -> None:
        assert _continuation_brief("原始描述", 0, 1) == "原始描述"

    def test_empty_brief_still_gets_a_hint(self) -> None:
        assert _continuation_brief("", 0, 2).startswith("（")


class TestToShotSpecsKeepsLongDurations:
    """`_to_shot_specs` **不能**按上限截断，否则拆分逻辑看不到"过长"这件事。"""

    def test_over_long_raw_duration_survives(self) -> None:
        from scidirector_ai.agents.director import _RawShot

        agent = make_agent()
        raw = [_RawShot(index=0, narration=LONG_NARRATION,
                        visual_brief="界面", tag="MOTION", duration_sec=40.0)]
        got = agent._to_shot_specs(raw, job_id="job-x")
        assert got[0].duration_sec == pytest.approx(40.0), (
            "截断成 15 秒等于悄悄裁掉 25 秒旁白，而且没有任何日志"
        )

    def test_lower_bound_is_still_enforced(self) -> None:
        from scidirector_ai.agents.director import _RawShot

        agent = make_agent()
        raw = [_RawShot(index=0, narration="短", visual_brief="x",
                        tag="MOTION", duration_sec=0.1)]
        got = agent._to_shot_specs(raw, job_id="job-x")
        assert got[0].duration_sec == MIN_SHOT_SEC


class TestDirectorPromptStatesTheCap:
    """提示词必须与程序里的常量一致，否则模型会输出被静默处理的值。"""

    def _prompt(self) -> str:
        return render_prompt("director", target_duration_sec=120)

    def test_states_the_new_cap(self) -> None:
        text = self._prompt()
        assert "15.0 秒" in text
        assert "25.0 秒" not in text, "上限已经降到 15 秒，提示词里不该留着 25"

    def test_warns_against_stretching_shots(self) -> None:
        text = self._prompt()
        # 必须点明"内容多就多切镜头"，而不是把镜头拉长。
        assert "多切" in text or "拆成" in text
        # 并说明原因（画面填不满），否则模型只会当成一条无理由的限制。
        assert "静止" in text or "节奏" in text

    def test_mentions_that_splitting_is_a_safety_net(self) -> None:
        text = self._prompt()
        assert "自动拆分" in text


class TestRepairInteractsWithTheCap:
    """放大后超出上限的部分必须靠**拆分**保住，不能被 clamp 丢掉。

    守的是一个真回归：上限从 25 降到 15 之后，`_repair_durations` 里那句
    `_clamp(..., MIN_SHOT_SEC, MAX_SHOT_SEC)` 会把放大后的镜头夹在 15.0 秒，
    而 15.0 恰好等于拆分阈值（`ceil(15/15 - ε) = 1`）→ **不触发拆分** →
    被夹掉的时长静默消失 → 实测目标 60 秒的任务只产出 **47.42 秒**。

    修法是让上限只由 `_split_long_shots` 负责：**拆分守恒**（各段之和 = 原时长），
    而 clamp 会丢。这条用例是当时那条集成测试的定向版本。
    """

    def test_total_stays_in_budget_when_scaling_up_hits_the_cap(self) -> None:
        agent = make_agent()
        # 3 个 15 秒镜头 = 45 秒，目标 60 秒：修复必须放大，
        # 放大后每个都超限 —— 只能靠拆分把多出来的时长保住。
        shots = [
            make_shot(duration_sec=15.0, shot_id=f"job-x-s{i:03d}", index=i)
            for i in range(3)
        ]
        got = agent._split_long_shots(shots, job_id="job-x")
        got = agent._repair_durations(got, 60.0, job_id="job-x")
        got = agent._split_long_shots(got, job_id="job-x")

        total = sum(s.duration_sec for s in got)
        assert 54.0 <= total <= 66.0, f"总时长必须落进目标 ±10%：{total}"
        assert all(s.duration_sec <= MAX_SHOT_SEC for s in got), (
            "每个镜头都不能超过上限"
        )

    def test_splitting_preserves_the_total(self) -> None:
        """**守恒**正是拆分能替代 clamp 的原因。"""
        agent = make_agent()
        got = agent._split_long_shots([make_shot(duration_sec=40.0)], job_id="job-x")
        assert abs(sum(s.duration_sec for s in got) - 40.0) < 0.05
