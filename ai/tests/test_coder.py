"""``CoderAgent`` 与编码提示词的测试。

两类重点：

1. **路由与静态门禁**：标签 -> 引擎 -> 提示词必须是确定性的；
   Manim 走 AST 白名单、HTML 走渲染契约检查，二者都不能漏。
2. **提示词契约**：占位符必须全部解析（残留的 ``{{...}}`` 会被模型照抄进输出），
   且提示词里声明的 JSON 字段必须与解析模型一致。

用桩 LLM 而非 mock 模式：mock 模式对任何引擎都返回同一段 Manim 代码，
无法验证"HTML 分支拿到 HTML"这类路由行为。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from scidirector_ai.agents.base import load_prompt
from scidirector_ai.agents.coder import (
    ENGINE_PROMPT_PLACEHOLDERS,
    PROMPT_BY_ENGINE,
    CodeArtifact,
    CoderAgent,
    _RawCode,
    load_engine_prompt,
    render_examples,
    title_from,
)
from scidirector_ai.config import Settings
from scidirector_ai.llm import LLMClient
from scidirector_ai.rag import FewShot, JsonCorpusRetriever
from scidirector_ai.schemas import RenderEngine, SceneTag, ShotSpec, StyleGuide

# ---------------------------------------------------------------------------
# 夹具与桩
# ---------------------------------------------------------------------------

VALID_MANIM = (
    "from manim import *\n\n\n"
    "class SciShotScene(Scene):\n"
    "    def construct(self):\n"
    "        title = Text('勾股定理', font_size=48)\n"
    "        self.play(Write(title), run_time=2)\n"
    "        self.wait(1)\n"
)
VALID_HTML = (
    "<div id='chart'></div>\n<script>\n"
    "  window.__seek = (t) => { document.body.dataset.t = String(t); };\n"
    "  window.__seek(0);\n  window.__ready = true;\n"
    "</script>"
)
DANGEROUS = "import os\nos.system('rm -rf /')\n"


class _StubLLM:
    """按顺序返回预设代码的桩 LLM。"""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def chat_json(self, system: str, user: str, schema: type, **kwargs: Any) -> Any:
        self.calls.append({"system": system, "user": user, **kwargs})
        code = self.responses.pop(0) if self.responses else ""
        return _RawCode(code=code, language="html+js", explanation="桩响应")


def make_settings(**overrides: object) -> Settings:
    return Settings(env="test", llm_provider="mock", **overrides)  # type: ignore[arg-type]


def make_agent(responses: list[str] | None = None, **overrides: object) -> CoderAgent:
    settings = make_settings(**overrides)
    agent = CoderAgent(LLMClient(settings), settings)
    if responses is not None:
        agent.llm = _StubLLM(responses)  # type: ignore[assignment]
    return agent


def make_shot(tag: SceneTag = SceneTag.MATH, **overrides: object) -> ShotSpec:
    base: dict[str, object] = {
        "shot_id": "job-x-s000",
        "index": 0,
        "narration": "从定义出发推导公式。",
        "visual_brief": "居中展示公式并逐项高亮。",
        "tag": tag,
        "duration_sec": 8.0,
        "keywords": ["公式", "推导"],
    }
    base.update(overrides)
    return ShotSpec(**base)  # type: ignore[arg-type]


def _code_of_length(n: int, *, marker: str = "") -> str:
    """造一段长度恰为 ``n`` 的代码，开头与结尾都带可断言的标记。"""
    head = "<!--HEAD-->"
    tail = "window.__seek = (t) => {};"
    mid_marker = marker
    body_len = max(n - len(head) - len(tail) - len(mid_marker), 0)
    return head + mid_marker + ("y" * body_len) + tail


class TestPreviousCodeInjection:
    """上一版代码必须**完整**进提示词，或至少截断得**响亮**。

    守的是一个真实缺陷：原实现是 ``previous_code[:6000]``，而实测生成过的 HTML
    动效代码最长到 **6681** 字符 —— 12 个镜头里有 4 个在"重做 / 修复"时看到的
    是残缺的上一版，而提示词却写着"重新输出完整代码"。模型于是很可能把没看到的
    那一段丢掉（往往正是收尾的 ``window.__seek`` 与闭合标签）。

    最糟的是它**没有任何日志**：截断是静默的，只表现为"打回重做效果不好"。
    """

    #: 实测生成过的最大 HTML 动效代码长度（来自真实任务的事件日志）。
    OBSERVED_MAX_CODE_CHARS = 6681

    def test_realistic_code_goes_in_verbatim(self) -> None:
        """**本条就是这个 bug 的回归测试**：真实长度的代码必须原样进提示词。"""
        agent = make_agent()
        code = _code_of_length(self.OBSERVED_MAX_CODE_CHARS)
        prompt = agent._build_user_prompt(
            shot=make_shot(tag=SceneTag.MOTION),
            style_guide=StyleGuide(),
            attempt=2,
            feedback_text="请把方块数量加到 8 个，并从四角向中心汇聚。",
            previous_code=code,
            examples=[],
        )
        assert "<!--HEAD-->" in prompt, "开头不见了"
        assert "window.__seek" in prompt, "结尾的渲染契约被截掉了"
        assert "省略" not in prompt, f"{self.OBSERVED_MAX_CODE_CHARS} 字符不该触发截断"

    def test_over_long_code_is_clipped_with_a_loud_marker(self) -> None:
        """真超长时也不许静默：必须显式说明省略了多少，且头尾都在。"""
        agent = make_agent()
        code = _code_of_length(90_000)
        prompt = agent._build_user_prompt(
            shot=make_shot(tag=SceneTag.MOTION),
            style_guide=StyleGuide(),
            attempt=2,
            feedback_text="改一下配色。",
            previous_code=code,
            examples=[],
        )
        assert "省略" in prompt, "截断必须显式标注，不能静默"
        assert "<!--HEAD-->" in prompt, "开头必须保留（结构与样式在那里）"
        assert "window.__seek" in prompt, "结尾必须保留（渲染契约在那里）"
        # 尾部那句提醒很重要：模型倾向于把"看不到的中间"连同结尾一起省掉。
        assert "闭合标签" in prompt

    def test_examples_are_not_silently_truncated(self) -> None:
        """RAG 范例代码同样不许静默截断。

        语料里 7 条范例有 2 条超过原来的 2500 字符上限（最长 3899）——
        也就是模型有时会拿到一份**残缺的范例**当作"参考写法"。
        残缺的范例比没有范例更糟：它可能照着学，把缺掉收尾
        （`window.__seek`、闭合标签）的写法一起学过去。
        """
        long_example = FewShot(
            id="data-002",
            tag="DATA",
            engine="d3",
            title="桩范例",
            summary="桩要点",
            code=_code_of_length(3_899),
        )
        rendered = render_examples([long_example])
        assert "window.__seek" in rendered, "范例的结尾被截掉了"
        assert "省略" not in rendered, "3899 字符不该触发截断"

    def test_repair_prompt_contains_the_code_only_once(self) -> None:
        """静态检查修复的提示词里，上一版代码只该出现一次。

        原实现把 ``artifact.code[:6000]`` 塞进 feedback，同时又把
        ``previous_code=artifact.code`` 传下去（进去再截一次），
        于是提示词里有两份**残缺**的代码 —— 既浪费上下文，
        又让"请重新输出完整代码"与它看到的残缺内容自相矛盾。
        """
        agent = make_agent()
        code = _code_of_length(8_000, marker="// UNIQUE_MARKER_XYZ\n")
        captured: dict[str, str] = {}

        def fake_call(engine: str, system: str, user: str) -> Any:
            captured["user"] = user
            return CodeArtifact(code="<ok>", language="html+js")

        agent._call_model = fake_call  # type: ignore[assignment]
        artifact = CodeArtifact(code=code, language="html+js")
        agent._repair(
            make_shot(tag=SceneTag.MOTION),
            StyleGuide(),
            artifact,
            "缺少 window.__seek",
            [],
            "（桩系统提示）",
        )

        user = captured["user"]
        assert user.count("UNIQUE_MARKER_XYZ") == 1, "上一版代码被注入了不止一次"
        assert "window.__seek" in user, "上一版代码必须真的在里面"


# ===========================================================================
# 路由
# ===========================================================================


class TestRouting:
    def test_every_llm_engine_has_a_prompt(self) -> None:
        from scidirector_ai.renderer import LLM_ENGINES

        missing = LLM_ENGINES - set(PROMPT_BY_ENGINE)
        assert not missing, f"这些引擎没有对应提示词：{sorted(missing)}"

    def test_prompt_mapping_is_deterministic(self) -> None:
        assert PROMPT_BY_ENGINE["manim"] == "coder_manim"
        assert PROMPT_BY_ENGINE["d3"] == "coder_html"
        assert PROMPT_BY_ENGINE["echarts"] == "coder_html"
        assert PROMPT_BY_ENGINE["code_anim"] == "coder_code_anim"

    def test_each_tag_selects_the_matching_system_prompt(self) -> None:
        """四种标签必须分别命中四个提示词分支（DATA 与 CODE 都走 HTML 系）。"""
        expectations = {
            SceneTag.MATH: "coder_manim",
            SceneTag.DATA: "coder_html",
            SceneTag.CODE: "coder_code_anim",
        }
        for tag, prompt_name in expectations.items():
            agent = make_agent([VALID_HTML])
            shot = make_shot(tag)
            agent.generate(shot=shot, style_guide=StyleGuide())

            system = agent.llm.calls[0]["system"]  # type: ignore[attr-defined]
            marker = load_prompt(prompt_name).splitlines()[0]
            assert marker in system, f"{tag} 没有使用 {prompt_name} 提示词"

    def test_stock_engine_skips_the_model_entirely(self) -> None:
        """``stock``（ffmpeg 渐变）由程序化生成 —— 不该浪费一次 LLM 调用。

        注意它现在**不是 AMBIENCE 的默认引擎**：环境镜头已改为走 HTML 动画，
        `stock` 只在 headless 浏览器不可用时由图节点降级使用
        （见 ``graph/nodes.py::_engine_or_fallback``）。
        所以这里必须显式指定引擎，而不是靠标签推导。
        """
        agent = make_agent([])
        shot = make_shot(SceneTag.AMBIENCE, keywords=[], engine=RenderEngine.STOCK)
        result = agent.generate(shot=shot, style_guide=StyleGuide())
        assert result.skipped_llm is True
        assert agent.llm.calls == []  # type: ignore[attr-defined]

    def test_ambience_still_uses_html_by_default(self) -> None:
        """环境镜头默认走 HTML 动画 —— 这正是"背景不再一成不变"的关键。"""
        shot = make_shot(SceneTag.AMBIENCE)
        assert shot.engine is RenderEngine.MOTION

    def test_stock_engine_produces_a_title_from_narration(self) -> None:
        agent = make_agent([])
        shot = make_shot(SceneTag.AMBIENCE,
                         narration="相对论的基本假设是光速不变。下一句不该出现。",
                         engine=RenderEngine.STOCK)
        result = agent.generate(shot=shot, style_guide=StyleGuide())
        assert result.overlay_text == "相对论的基本假设是光速不变"
        assert "下一句" not in result.overlay_text

    def test_explicit_overlay_text_wins(self) -> None:
        agent = make_agent([])
        shot = make_shot(SceneTag.AMBIENCE,
                         narration="画外音", meta={"overlay_text": "指定标题"},
                         engine=RenderEngine.STOCK)
        assert agent.generate(shot=shot, style_guide=StyleGuide()).overlay_text == "指定标题"


# ===========================================================================
# 静态门禁
# ===========================================================================


class TestStaticGate:
    def test_manim_code_uses_ast_policy(self) -> None:
        agent = make_agent([VALID_MANIM])
        result = agent.generate(shot=make_shot(SceneTag.MATH), style_guide=StyleGuide())
        assert result.policy_ok is True
        assert result.llm_attempts == 1

    def test_dangerous_manim_code_is_rejected_after_repair(self) -> None:
        """危险代码必须被拦下；自动修复一次仍失败则如实标记为不通过。"""
        agent = make_agent([DANGEROUS, DANGEROUS])
        result = agent.generate(shot=make_shot(SceneTag.MATH), style_guide=StyleGuide())

        assert result.policy_ok is False
        assert "os" in result.policy_summary
        assert result.llm_attempts == 2, "应当尝试过自动修复"

    def test_repair_can_fix_the_code(self) -> None:
        """第一次给危险代码、第二次给合法代码 -> 最终通过。"""
        agent = make_agent([DANGEROUS, VALID_MANIM])
        result = agent.generate(shot=make_shot(SceneTag.MATH), style_guide=StyleGuide())
        assert result.policy_ok is True
        assert result.llm_attempts == 2
        # 修复请求里必须带上具体的违规信息，否则模型只能盲改。
        repair_user = agent.llm.calls[1]["user"]  # type: ignore[attr-defined]
        assert "静态安全检查未通过" in repair_user

    def test_html_without_seek_is_rejected(self) -> None:
        """缺少 window.__seek 的 HTML 会导致"渲染成功但画面静止"，
        属于**静默失败**，必须在渲染前拦下。"""
        agent = make_agent(["<div>静态内容</div>", "<div>还是静态</div>"])
        result = agent.generate(shot=make_shot(SceneTag.DATA), style_guide=StyleGuide())
        assert result.policy_ok is False
        assert "window.__seek" in result.policy_summary

    def test_html_with_cdn_is_rejected(self) -> None:
        agent = make_agent([VALID_HTML.replace("<div id='chart'></div>",
                                               "<script src='https://cdn.jsdelivr.net/d3.min.js'></script>")] * 2)
        result = agent.generate(shot=make_shot(SceneTag.DATA), style_guide=StyleGuide())
        assert result.policy_ok is False
        assert "CDN" in result.policy_summary

    def test_valid_html_passes(self) -> None:
        agent = make_agent([VALID_HTML])
        result = agent.generate(shot=make_shot(SceneTag.DATA), style_guide=StyleGuide())
        assert result.policy_ok is True

    def test_empty_code_raises(self) -> None:
        from scidirector_ai.llm import LLMError

        agent = make_agent([""])
        with pytest.raises(LLMError):
            agent.generate(shot=make_shot(SceneTag.MATH), style_guide=StyleGuide())


# ===========================================================================
# RAG 与反馈注入
# ===========================================================================


class TestPromptInjection:
    def test_examples_are_injected(self) -> None:
        """召回范例必须真的进了用户提示词 —— 否则 RAG 只是空跑。"""
        agent = make_agent([VALID_MANIM])
        agent.generate(shot=make_shot(SceneTag.MATH), style_guide=StyleGuide())

        user = agent.llm.calls[0]["user"]  # type: ignore[attr-defined]
        assert "参考范例" in user
        assert "公式逐步高亮推导" in user, "没有召回到 math-001"

    def test_no_examples_is_stated_explicitly(self) -> None:
        """没召回时要**显式说明**，而不是留一片空白让模型猜。"""
        agent = make_agent([VALID_MANIM])
        agent.retriever = JsonCorpusRetriever(())  # type: ignore[assignment]
        agent.generate(shot=make_shot(SceneTag.MATH), style_guide=StyleGuide())
        user = agent.llm.calls[0]["user"]  # type: ignore[attr-defined]
        assert "没有召回到参考范例" in user

    def test_feedback_is_injected_on_retry(self) -> None:
        agent = make_agent([VALID_MANIM])
        agent.generate(
            shot=make_shot(SceneTag.MATH),
            style_guide=StyleGuide(),
            attempt=2,
            feedback_text="【必须落实的修改】\n- 把字号从 24 提到 48",
            previous_code="class Old(Scene): pass",
        )
        user = agent.llm.calls[0]["user"]  # type: ignore[attr-defined]
        assert "上一轮反馈（必须逐条处理）" in user
        assert "把字号从 24 提到 48" in user
        assert "上一版代码" in user

    def test_style_guide_is_injected(self) -> None:
        agent = make_agent([VALID_MANIM])
        agent.generate(
            shot=make_shot(SceneTag.MATH),
            style_guide=StyleGuide(background_color="#123456", min_font_size=48),
        )
        user = agent.llm.calls[0]["user"]  # type: ignore[attr-defined]
        assert "#123456" in user
        assert "48" in user

    def test_task_hint_is_passed(self) -> None:
        """必须携带显式任务标识，mock 模式靠它返回正确的结构。"""
        from scidirector_ai.llm import Task

        agent = make_agent([VALID_MANIM])
        agent.generate(shot=make_shot(SceneTag.MATH), style_guide=StyleGuide())
        assert agent.llm.calls[0]["task"] == Task.CODE  # type: ignore[attr-defined]


# ===========================================================================
# 提示词契约
# ===========================================================================

_UNRESOLVED = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_JSON_BLOCK = re.compile(r"```json\s*(.*?)```", re.DOTALL)


def render_engine_prompt(name: str, **overrides: object) -> str:
    settings = make_settings()
    kwargs: dict[str, Any] = {
        "min_font_size": 36,
        "duration_sec": 8.0,
        "background_color": "#0B1020",
        "primary_color": "#4F8CFF",
    }
    kwargs.update(overrides)
    return load_engine_prompt(name, settings, **kwargs)


#: 所有引擎提示词。**从 ``PROMPT_BY_ENGINE`` 派生**，而不是手写清单 ——
#: 手写清单会在新增引擎时悄悄过期，而"新提示词没有被契约测试覆盖"
#: 恰恰是这一组用例要防的事（d3 与 echarts 共用一份提示词，故先去重）。
ENGINE_PROMPTS: list[str] = sorted(set(PROMPT_BY_ENGINE.values()))


class TestEnginePromptContract:
    @pytest.mark.parametrize("name", ENGINE_PROMPTS)
    def test_all_placeholders_resolve(self, name: str) -> None:
        """渲染后不得残留 ``{{...}}`` —— 残留会被模型原样抄进代码。"""
        rendered = render_engine_prompt(name)
        leftover = _UNRESOLVED.findall(rendered)
        assert not leftover, f"{name} 残留未解析的占位符：{leftover}"

    @pytest.mark.parametrize("name", ENGINE_PROMPTS)
    def test_real_values_are_injected(self, name: str) -> None:
        rendered = render_engine_prompt(name, min_font_size=44, duration_sec=12.5,
                                        background_color="#ABCDEF")
        assert "44" in rendered, f"{name} 未注入字号下限"
        assert "#ABCDEF" in rendered, f"{name} 未注入背景色"
        assert "12.5" in rendered, f"{name} 未注入时长"

    def test_placeholder_list_matches_the_prompts(self) -> None:
        """``ENGINE_PROMPT_PLACEHOLDERS`` 必须覆盖提示词里真实用到的占位符。

        防止有人加了新占位符却忘了在注入端登记 —— 那种情况会在渲染后
        留下 ``{{...}}``，而模型会把它当成真实语法抄进代码。
        """
        for name in ENGINE_PROMPTS:
            used = set(_UNRESOLVED.findall(load_prompt(name)))
            unknown = used - set(ENGINE_PROMPT_PLACEHOLDERS)
            assert not unknown, f"{name} 使用了未登记的占位符：{sorted(unknown)}"

    @pytest.mark.parametrize("name", ["coder_manim", "coder_html", "coder_code_anim"])
    def test_prompt_declares_json_output(self, name: str) -> None:
        rendered = render_engine_prompt(name)
        block = _JSON_BLOCK.search(rendered)
        assert block, f"{name} 没有 JSON 输出示例"
        parsed = json.loads(block.group(1))
        assert set(parsed) == {"code", "language", "explanation"}
        assert set(parsed) == set(_RawCode.model_fields), (
            f"{name} 的 JSON 示例字段与 _RawCode 不一致"
        )

    def test_manim_prompt_states_safety_rules(self) -> None:
        rendered = render_engine_prompt("coder_manim")
        assert "SciShotScene" in rendered
        assert "禁止" in rendered
        assert "import os" in rendered, "没有明确列出禁止的导入"
        assert "run_time" in rendered, "没有给出动画时长的下限约束"

    def test_html_prompt_states_the_seek_contract(self) -> None:
        rendered = render_engine_prompt("coder_html")
        assert "window.__seek" in rendered
        assert "window.__ready" in rendered
        assert "逐帧截图" in rendered, "没有解释为什么不能用 CSS transition"
        assert "CDN" in rendered, "没有明确禁止引用 CDN"

    def test_code_anim_prompt_states_typing_rules(self) -> None:
        rendered = render_engine_prompt("coder_code_anim")
        assert "window.__seek" in rendered
        assert "setInterval" in rendered, "没有明确禁止用 setInterval 做光标闪烁"
        assert "monospace" in rendered, "没有要求等宽字体"

    @pytest.mark.parametrize("name", ["coder_manim", "coder_html", "coder_code_anim"])
    def test_prompts_are_chinese_and_substantial(self, name: str) -> None:
        rendered = render_engine_prompt(name)
        han = sum(1 for ch in rendered if "\u4e00" <= ch <= "\u9fff")
        assert han > 400, f"{name} 中文内容过少（{han} 字）"
        assert len(rendered) > 1500, f"{name} 过短，不足以约束模型"


class TestUserPromptTemplate:
    def test_all_placeholders_resolve(self) -> None:
        from scidirector_ai.agents.base import render_prompt

        rendered = render_prompt(
            "coder_user", index=1, tag="MATH", engine="manim", duration_sec=8.0,
            narration="n", visual_brief="v", keywords="k", style_guide="s",
            examples="e", feedback_block="",
        )
        assert not _UNRESOLVED.findall(rendered)

    def test_declares_output_constraint(self) -> None:
        rendered = load_prompt("coder_user")
        assert "只输出 JSON" in rendered


# ===========================================================================
# 工具函数
# ===========================================================================


class TestTitleFrom:
    @pytest.mark.parametrize(
        ("narration", "expected"),
        [
            ("相对论的基本假设是光速不变。下一句。", "相对论的基本假设是光速不变"),
            ("第一句！第二句。", "第一句"),
            ("问句吗？后面还有。", "问句吗"),
            ("只有一句话没有标点", "只有一句话没有标点"),
            # 冒号同样是分句点：标题取 `勾股定理`，而不是把整句解释硬截半句。
            # 少了这条，标题会变成 `勾股定理：直角三角形两条直角…` ——
            # VLM 审查如实把它记为"文字被截断"，而那确实是观感缺陷。
            ("勾股定理：直角三角形两条直角边的平方和等于斜边的平方。", "勾股定理"),
            ("", ""),
            ("   ", ""),
        ],
    )
    def test_splits_on_first_sentence(self, narration: str, expected: str) -> None:
        assert title_from(narration) == expected

    def test_truncates_long_titles(self) -> None:
        """超出上限时截断并加省略号 —— 否则小屏上会换行到第三行破坏构图。"""
        title = title_from("这是一个非常长的标题" * 5, limit=10)
        assert len(title) == 10
        assert title.endswith("…")

    def test_does_not_cut_within_limit(self) -> None:
        assert title_from("短标题", limit=10) == "短标题"


class TestRenderExamples:
    def test_empty_list_is_explicit(self) -> None:
        assert "没有召回" in render_examples([])

    def test_includes_title_summary_and_code(self) -> None:
        example = FewShot(id="a", title="标题", tag="MATH", engine="manim",
                          summary="要点说明", code="print(1)")
        rendered = render_examples([example])
        assert "标题" in rendered and "要点说明" in rendered and "print(1)" in rendered

    def test_long_example_is_kept_whole_or_explicitly_marked(self) -> None:
        """**这条改写了旧契约。** 旧契约是"超过 2500 就截断"。

        而语料里 7 条范例有 2 条超过 2500（最长 3899）—— 于是模型有时会拿到
        一份残缺的范例当作"参考写法"，那比不给范例更糟：它可能照着学，
        把缺掉收尾（`window.__seek`、闭合标签）的写法一起学过去。

        新契约：上限之内**原样**注入；真超限则头尾保留并显式标注省略量。
        """
        five_k = FewShot(id="a", title="t", tag="MATH", engine="manim",
                         summary="s", code="x" * 5_000)
        rendered = render_examples([five_k])
        assert "省略" not in rendered, "5000 字符在上限之内，不该截断"

        huge = FewShot(id="b", title="t", tag="MATH", engine="manim",
                       summary="s", code="y" * 90_000)
        assert "省略" in render_examples([huge]), "超限必须显式标注，不能静默"


class TestCodeArtifact:
    def test_defaults(self) -> None:
        artifact = CodeArtifact()
        assert artifact.code == "" and artifact.language == "python"
        assert artifact.explanation == ""


class TestMotionPacingContract:
    """动效提示词必须要求**把整段时长铺满**。

    守的是一条真实事故（job-afdfcd4350073365-s000）：22.83 秒的镜头，
    模型把界面搭好、动画演了约 3 秒就"完成"，之后 18 秒画面几乎不动。
    实测相邻抽帧的**变化像素占比**一路塌到 0.06~0.19%（全画面基本静止），
    审查连续四轮（**正确地**）判它"动画停滞"，而反馈给的
    "把打字 run_time 从 0.5 延长到 3 秒"这类微调根本填不满那段时间 ——
    于是代码改来改去画面不变、审查结论一字不差，一直烧到人工介入。

    原提示词里只有"一个镜头只讲一个动作：出现 → 变化 → **停住**"，
    等于在教模型"演完就停"。这条用例钉住后来补的「时长铺满」要求。
    """

    def test_motion_prompt_requires_filling_the_duration(self) -> None:
        prompt = render_engine_prompt("coder_motion")
        assert "时长铺满" in prompt, "缺少'铺满时长'这一节"
        # 关键判据：审查是等间隔抽帧比对，所以要求必须按"帧与帧之间"表述。
        assert "抽" in prompt and "帧" in prompt
        # 必须给出可操作的做法（分阶段），而不只是"要有节奏"这种口号。
        assert "阶段" in prompt

    def test_motion_prompt_explains_static_hold_is_not_pacing(self) -> None:
        """表达"卡住"也不能用静止画面 —— 那是审查抓得最准的一类问题。"""
        prompt = render_engine_prompt("coder_motion")
        assert "卡住" in prompt
        assert "静止画面" in prompt

    def test_motion_prompt_keeps_the_final_state_to_the_end(self) -> None:
        prompt = render_engine_prompt("coder_motion")
        assert "最后" in prompt
        # 实测只写"留到最后"不够：模型仍会在约 70% 处进入完成态然后干等，
        # 所以提示词要求"最后一个阶段本身仍在进行中"。
        assert "进行中" in prompt or "仍在" in prompt
