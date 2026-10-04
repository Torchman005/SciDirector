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
    # 容差下限：低于下限但仍在它的 90% 以内算"偏小但可读"，不判负。
    # 没有这条，差 1px 就会触发一整轮重渲染。
    "min_font_size_tolerance": 32,
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
    # 相邻抽帧的时间间隔。这条决定了 VLM**有没有资格**判断连续运动：
    # 22.83 秒的镜头只抽 4 张，间隔约 5.7 秒 —— 而它连续四轮要求
    # "风扇持续旋转（约 4.2 秒一圈）"，从这些静帧里根本判断不了。
    "frame_interval_sec": "1.6",
    # 抽帧缩放宽度与"成片/缩略图"比例。审查提示词必须把它们告诉 VLM，
    # 否则它会拿缩略图上的字号去对成片像素的阈值，系统性索要过大字号。
    "preview_width": 1024,
    "preview_scale": "1.88",
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

    def test_font_size_judgement_has_a_tolerance_band(self, critic_system_prompt: str) -> None:
        """字号判定必须分两档，不能为 1~2px 触发一整轮重渲染。

        真实代价：某镜头在缩略图上量到 16px、标准是 17px，连续三轮判负 ——
        每轮都白烧一次 1080p 渲染加一次 VLM 调用，最后仍然转人工，
        而画面质量并没有因此变好。可读性的红线应当拦"明显读不清"。
        """
        assert "32px" in critic_system_prompt, "容差下限没有注入"
        assert "10%" in critic_system_prompt, "没有说明容差的比例"

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

    def test_user_template_states_the_preview_scale(self, critic_user_prompt: str) -> None:
        """必须给出缩略图与成片的比例。

        抽帧会被缩放到 1024px 宽再送审，而系统提示词里的字号下限是**成片像素**。
        不说换算比例，VLM 就会拿缩略图上的字号去对成片阈值 ——
        1080p 下 32px 在它眼里只有 17px，于是它一路要求"提到 48px"，
        每次都白烧一轮 1080p 渲染加一次模型调用。
        """
        assert "1024" in critic_user_prompt, "没有告诉模型缩略图宽度"
        assert "1.88" in critic_user_prompt, "没有告诉模型换算比例"
        assert "成片" in critic_user_prompt, "没有说明阈值是成片像素"


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


class TestRenderContractAndRoundConsistency:
    """审查必须知道**哪些手段在这个引擎里根本用不了**，以及**不许自相矛盾**。

    这一组守的是真实事故（job-afdfcd4350073365-s000）：
      * 我们的动效提示词明令禁止 CSS animation / setTimeout / rAF
        （HTML 引擎是"逐帧求值 + 截图"，画面只能由 `window.__seek(t)` 决定）；
      * 而审查连续四轮要求"为风扇图标添加 CSS 动画 animation: spin 1s linear infinite"；
      * 编码智能体**不能照做**（会被静态检查拦下），于是那一轮"实际没有任何改动"；
      * 审查下一轮看到同样的画面，再说一遍同样的话 —— 一直烧到人工介入。

    同一事故里审查还**自相矛盾**：前三轮要求"逐字打字 3.0 秒"，第四轮要求
    "逐字打字在 2 帧内完成（约 0.067 秒）"；前三轮要求"90% 处闪烁"，
    第四轮要求"90% 处静止 10 秒"。编码端只能满足其中一条。
    """

    def test_forbids_suggesting_css_animations(self, critic_system_prompt: str) -> None:
        text = critic_system_prompt
        assert "animation" in text, "必须点名 CSS animation，否则模型仍会建议它"
        assert "setTimeout" in text and "requestAnimationFrame" in text
        assert "__seek" in text, "要说清 HTML 引擎是逐帧求值，画面只由 t 决定"
        # 必须给出"改成描述画面效果"的替代写法，否则模型只是被禁止而不知所措。
        assert "画面效果" in text or "观众应该看到什么" in text

    def test_warns_that_still_frames_cannot_prove_motion(
        self, critic_system_prompt: str
    ) -> None:
        text = critic_system_prompt
        assert "采样间隔" in text, "必须告诉它间隔这件事，它才知道自己判不了"
        assert "静帧" in text
        # 关键要求：证不了就别写进判定理由 —— 否则会给出永远无法满足的意见。
        assert "无法被满足" in text or "不要" in text

    def test_forbids_contradicting_previous_suggestions(
        self, critic_system_prompt: str
    ) -> None:
        text = critic_system_prompt
        assert "自相矛盾" in text
        assert "上一轮" in text
        # 已修好的项必须承认，不能因为"上一轮说过"就继续扣分。
        assert "已修复" in text or "已经改好" in text

    def test_user_prompt_states_the_sampling_interval(
        self, critic_user_prompt: str
    ) -> None:
        assert "1.6" in critic_user_prompt, "采样间隔必须真的渲染进提示词"
        assert "{{" not in critic_user_prompt


#: 生产代码**实际**传给系统提示词的变量（见 critic.py 的 render_prompt("critic", ...)）。
#:
#: 与 PLACEHOLDER_VALUES 的区别很关键：后者是"所有模板用到的值"的**并集**，
#: 用它渲染会**掩盖**"某个模板引用了一个生产根本没传的变量"这类错误 ——
#: 那种错误在生产里表现为提示词里留着一个字面的 `{{duration_sec}}`，
#: 而测试全绿。所以这里单独维护一份"生产真实值"。
PRODUCTION_SYSTEM_VALUES: dict[str, Any] = {
    "threshold": "0.75",
    "background_color": "#0B1020",
    "min_font_size": 36,
    "min_font_size_tolerance": 32,
    "duration_sec": 22.83,
    "frame_interval_sec": "4.6",
}


class TestProductionVariablesAreEnough:
    """提示词必须能被**生产实际传的变量**渲染干净。

    守的是一个刚踩过的坑：在 critic.md 里加了 {{duration_sec}} / {{frame_interval_sec}}，
    却忘了在 critic.py 的 render_prompt("critic", ...) 里补上 ——
    生产里会留下字面占位符，而用 PLACEHOLDER_VALUES 并集渲染的测试照样全绿。
    """

    def test_system_prompt_resolves_with_production_values_only(self) -> None:
        rendered = render_prompt("critic", **PRODUCTION_SYSTEM_VALUES)
        leftover = _UNRESOLVED.findall(rendered)
        assert not leftover, (
            f"系统提示词里残留了 {leftover} —— 生产没传这些变量，"
            "模型会看到字面的花括号占位符"
        )

    def test_system_prompt_carries_the_shot_duration(self) -> None:
        """时长必须真的出现在系统提示词里。

        第四节"建议的量级要配得上问题的量级"必须能引用具体秒数，
        否则模型不知道这个镜头有多长、也就判断不了"动画是不是早早演完了"。
        """
        rendered = render_prompt("critic", **PRODUCTION_SYSTEM_VALUES)
        assert "22.83" in rendered
        assert "4.6" in rendered
