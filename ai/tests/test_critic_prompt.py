"""审查智能体提示词的**契约测试**。

提示词是一种代码资产，但它没有类型检查、没有编译错误 ——
有人不小心删掉 JSON schema 或"必须给可执行建议"那一段，
在运行到线上之前不会有任何提示。这组测试就是它的编译器。

守护四件事：
1. 提示词能通过**真实的加载器**装载与渲染（占位符全部可解析）；
2. 需求要求的硬性内容存在（严格 JSON、`passed` 字段、具体修改建议）；
3. **提示词里的 JSON 示例与代码里的 :class:`CriticFeedback` schema 不漂移**
   —— 这是最有价值的一条：模型输出与解析模型一旦对不上，
   表现为"模型明明答对了却解析失败"，且极难定位；
4. 提示词自身自洽（示例遵守它自己定的规则）。
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from scidirector_ai.agents.base import load_prompt, render_prompt
from scidirector_ai.config import get_settings
from scidirector_ai.schemas import CriticFeedback

#: 渲染提示词时为占位符提供的样例值。
#: 新增占位符时必须同步在这里补上 —— 否则下面的"占位符全部解析"测试会失败，
#: 这正是我们想要的效果：忘记补值时**立刻**被发现，而不是等运行时漏进提示词里。
PLACEHOLDER_VALUES: dict[str, Any] = {
    "threshold": "0.75",
    "background_color": "#0B1020",
    "min_font_size": 36,
    "index": 2,
    "tag": "MATH",
    "engine": "manim",
    "duration_sec": 8.0,
    "actual_duration": 7.9,
    "width": 1920,
    "height": 1080,
    "attempt": 2,
    "narration": "把等号两侧同时平方。",
    "visual_brief": "居中展示公式，逐步高亮等号两侧。",
    "style_guide": "- 背景色：#0B1020",
    "previous_feedback": "",
    "frame_count": 6,
}

#: 未被解析的 Jinja 风格占位符。渲染后不应残留。
_UNRESOLVED = re.compile(r"\{\{\s*(\w+)\s*\}\}")

#: 提取 markdown 里的 ```json 代码块。
_JSON_BLOCK = re.compile(r"```json\s*(.*?)```", re.DOTALL)


@pytest.fixture(scope="module")
def critic_system_prompt() -> str:
    return render_prompt("critic", **PLACEHOLDER_VALUES)


@pytest.fixture(scope="module")
def critic_user_prompt() -> str:
    return render_prompt("critic_user", **PLACEHOLDER_VALUES)


# ===========================================================================
# 1. 可装载 / 可渲染
# ===========================================================================


class TestLoadability:
    def test_prompt_files_exist(self) -> None:
        prompts_dir = get_settings().resolved_prompts_dir
        assert (prompts_dir / "critic.md").is_file()
        assert (prompts_dir / "critic_user.md").is_file()

    def test_system_prompt_is_substantial(self, critic_system_prompt: str) -> None:
        """详细提示词不能退化成一两句话。"""
        assert len(critic_system_prompt) > 2000, "系统提示词过短，不足以约束模型行为"

    def test_all_placeholders_resolve(self, critic_system_prompt: str) -> None:
        """渲染后不得残留任何 ``{{...}}``。

        残留的占位符会被模型原样看到，并且它会**照抄**到输出里 ——
        这是那种"看起来跑通了、结果全是垃圾"的故障。
        """
        leftover = _UNRESOLVED.findall(critic_system_prompt)
        assert not leftover, f"系统提示词中存在未解析的占位符：{leftover}"

    def test_user_placeholders_resolve(self, critic_user_prompt: str) -> None:
        leftover = _UNRESOLVED.findall(critic_user_prompt)
        assert not leftover, f"用户提示词中存在未解析的占位符：{leftover}"

    def test_injected_values_actually_appear(self, critic_system_prompt: str) -> None:
        """注入的值必须真的出现在提示词里 —— 防止占位符写错名字后被静默忽略。"""
        assert "0.75" in critic_system_prompt, "阈值未注入"
        assert "#0B1020" in critic_system_prompt, "背景色未注入"
        assert "36px" in critic_system_prompt, "字号下限未注入"

    def test_prompt_is_chinese(self, critic_system_prompt: str) -> None:
        """需求明确要求中文提示词。"""
        han = sum(1 for ch in critic_system_prompt if "\u4e00" <= ch <= "\u9fff")
        assert han > 800, f"中文字符仅 {han} 个，提示词可能不是中文写的"


# ===========================================================================
# 2. 需求要求的硬性内容
# ===========================================================================


class TestRequiredContent:
    def test_demands_strict_json(self, critic_system_prompt: str) -> None:
        assert "JSON" in critic_system_prompt
        # 必须明确禁止 JSON 之外的输出，否则模型会加一堆解释性前言。
        assert "只输出" in critic_system_prompt

    def test_defines_passed_field(self, critic_system_prompt: str) -> None:
        assert '"passed"' in critic_system_prompt
        assert "true" in critic_system_prompt and "false" in critic_system_prompt

    def test_defines_suggestions_field(self, critic_system_prompt: str) -> None:
        assert '"suggestions"' in critic_system_prompt

    def test_explains_when_passed_is_true(self, critic_system_prompt: str) -> None:
        """必须给出**判定规则**，而不是只说"你觉得行就行"。

        没有明确规则时，模型会随心情给结论，重试成本完全不可控。
        """
        assert "当且仅当" in critic_system_prompt
        assert "logic_score" in critic_system_prompt

    def test_requires_actionable_suggestions(self, critic_system_prompt: str) -> None:
        """最关键的约束：不通过时必须给出可执行的修改指令。"""
        assert "可执行" in critic_system_prompt
        assert "至少一条" in critic_system_prompt

    def test_has_good_and_bad_examples(self, critic_system_prompt: str) -> None:
        """只讲"要写具体"没有用，必须给出对照示例。

        模型对抽象要求的服从度远低于对示例的模仿。
        """
        assert "字号从 24 提到 48" in critic_system_prompt
        assert "字号太小了" in critic_system_prompt

    def test_defines_the_rubric_dimensions(self, critic_system_prompt: str) -> None:
        for dim in ("logic_score", "readability_score", "pacing_score", "aesthetics_score"):
            assert dim in critic_system_prompt, f"缺少评分维度 {dim}"

    def test_forbids_fabricating_issues(self, critic_system_prompt: str) -> None:
        """必须明确禁止"为了显得严格而编造问题"。

        凭空造出的问题会触发一轮毫无必要的重渲染 —— 这是实打实的成本。
        """
        assert "编造" in critic_system_prompt

    def test_gives_guidance_when_uncertain(self, critic_system_prompt: str) -> None:
        """必须说明"拿不准时怎么办"，否则模型会硬猜一个结论。"""
        assert "拿不准" in critic_system_prompt or "无法判断" in critic_system_prompt

    def test_user_template_asks_for_json(self, critic_user_prompt: str) -> None:
        assert "JSON" in critic_user_prompt

    def test_user_template_lists_frames_in_order(self, critic_user_prompt: str) -> None:
        """必须告诉模型抽帧的**时间顺序**，否则它无法判断动画节奏。"""
        assert "首帧" in critic_user_prompt and "末帧" in critic_user_prompt


# ===========================================================================
# 3. 与代码 schema 不漂移（最有价值的一组）
# ===========================================================================


def _extract_json_example(prompt: str) -> dict[str, Any]:
    """从提示词里取出那个 JSON 示例。"""
    blocks = _JSON_BLOCK.findall(prompt)
    assert blocks, "提示词里没有 json 代码块示例"
    for block in blocks:
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "passed" in parsed:
            return parsed
    raise AssertionError("提示词里的 json 代码块无法解析，或缺少 passed 字段")


class TestSchemaAlignment:
    def test_example_is_valid_json(self, critic_system_prompt: str) -> None:
        example = _extract_json_example(critic_system_prompt)
        assert isinstance(example["passed"], bool)

    def test_example_keys_match_critic_feedback_schema(
        self, critic_system_prompt: str
    ) -> None:
        """**核心断言**：提示词示例的字段必须都是 :class:`CriticFeedback` 的字段。

        漂移的后果很难查：模型按提示词输出，而解析模型不认识那个字段，
        表现为"模型明明答对了却解析失败"，然后白白重试一次。
        """
        example = _extract_json_example(critic_system_prompt)
        model_fields = set(CriticFeedback.model_fields)
        unknown = set(example) - model_fields
        assert not unknown, (
            f"提示词示例包含 CriticFeedback 中不存在的字段：{sorted(unknown)}；"
            f"可用字段：{sorted(model_fields)}"
        )

    def test_example_validates_against_schema(self, critic_system_prompt: str) -> None:
        """示例必须能真正通过 Pydantic 校验。

        这同时验证了 schema 的业务约束（"不通过必须有可执行建议"）
        与提示词的示例是自洽的 —— 示例本身就违反规则的话，
        模型必然会照抄出一个违反规则的输出。
        """
        example = _extract_json_example(critic_system_prompt)
        feedback = CriticFeedback.model_validate(example)
        assert feedback.passed is False
        assert feedback.suggestions, "示例是不通过却没有建议，违反自身规则"
        assert 0.0 <= feedback.score <= 1.0

    def test_scoring_fields_in_prompt_exist_in_schema(self, critic_system_prompt: str) -> None:
        """提示词提到的每个评分字段都必须在 schema 里存在。"""
        mentioned = set(re.findall(r"\b(logic_score|readability_score|pacing_score|"
                                 r"aesthetics_score|score|issues|suggestions|passed)\b",
                                 critic_system_prompt))
        missing = mentioned - set(CriticFeedback.model_fields)
        assert not missing, f"提示词提到但 schema 里没有的字段：{sorted(missing)}"

    def test_dimension_weights_sum_to_one(self, critic_system_prompt: str) -> None:
        """加权公式的权重必须加起来等于 1。

        不等于 1 会怎样：总分被系统性放大或缩小，
        阈值 0.75 的实际含义随之漂移，而这是**看不出来**的 ——
        只会表现为"最近通过率莫名变高/变低"。
        """
        weights = [float(w) for w in re.findall(r"×\s*(0\.\d+)", critic_system_prompt)]
        assert len(weights) == 4, f"未找到四个维度的权重，实际找到 {weights}"
        assert abs(sum(weights) - 1.0) < 1e-9, f"权重之和为 {sum(weights)}，应为 1.0"

    def test_threshold_flows_into_the_rule(self, critic_system_prompt: str) -> None:
        """阈值必须是注入的，不能写死在提示词里。

        写死会怎样：配置里改了阈值，提示词里还是旧值，
        模型按旧阈值判断、程序按新阈值判定，两边规则不一致。
        """
        assert "≥ 0.75" in critic_system_prompt

    def test_user_template_keys_are_renderable(self) -> None:
        """用户模板引用的占位符必须都在样例值表里 —— 防止运行时漏传。"""
        raw = load_prompt("critic_user")
        required = set(_UNRESOLVED.findall(raw))
        missing = required - set(PLACEHOLDER_VALUES)
        assert not missing, f"用户模板引用了未提供样例值的占位符：{sorted(missing)}"

    def test_system_template_keys_are_renderable(self) -> None:
        raw = load_prompt("critic")
        required = set(_UNRESOLVED.findall(raw))
        missing = required - set(PLACEHOLDER_VALUES)
        assert not missing, f"系统提示词引用了未提供样例值的占位符：{sorted(missing)}"


# ===========================================================================
# 4. 提示词自洽性
# ===========================================================================


class TestSelfConsistency:
    def test_pass_example_would_have_no_suggestions(self, critic_system_prompt: str) -> None:
        """提示词必须明确要求"通过时 suggestions 为空数组"。

        不要求的话，模型常会在通过时也塞几条"可以更好"的建议，
        下游若按"有建议即重做"处理，就会无限重做已经合格的镜头。
        """
        assert "通过时" in critic_system_prompt and "空数组" in critic_system_prompt

    def test_declares_fatal_issues(self, critic_system_prompt: str) -> None:
        """必须列出"致命问题"清单，避免模型对明显失败的作品还给中间分。"""
        assert "致命问题" in critic_system_prompt

    def test_limits_suggestion_count(self, critic_system_prompt: str) -> None:
        """建议数量要有上限，否则模型会一次列十几条，重写时顾此失彼。"""
        assert "最多 6 条" in critic_system_prompt

    def test_instructs_verifying_previous_feedback(self, critic_system_prompt: str) -> None:
        """重试场景必须确认上一轮问题是否已修复。

        不要求的话，模型会给出与上一轮几乎相同的意见，
        循环就永远收敛不了。
        """
        assert "上一轮" in critic_system_prompt
