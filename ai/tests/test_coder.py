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
from scidirector_ai.schemas import SceneTag, ShotSpec, StyleGuide

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

    def test_ambience_skips_the_model_entirely(self) -> None:
        """氛围镜头由 ffmpeg 程序化生成 —— 不该浪费一次 LLM 调用。"""
        agent = make_agent([])
        result = agent.generate(shot=make_shot(SceneTag.AMBIENCE, keywords=[]), style_guide=StyleGuide())
        assert result.skipped_llm is True
        assert agent.llm.calls == []  # type: ignore[attr-defined]

    def test_ambience_produces_a_title_from_narration(self) -> None:
        agent = make_agent([])
        shot = make_shot(SceneTag.AMBIENCE,
                         narration="相对论的基本假设是光速不变。下一句不该出现。")
        result = agent.generate(shot=shot, style_guide=StyleGuide())
        assert result.overlay_text == "相对论的基本假设是光速不变"
        assert "下一句" not in result.overlay_text

    def test_explicit_overlay_text_wins(self) -> None:
        agent = make_agent([])
        shot = make_shot(SceneTag.AMBIENCE,
                         narration="画外音", meta={"overlay_text": "指定标题"})
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


class TestEnginePromptContract:
    @pytest.mark.parametrize("name", ["coder_manim", "coder_html", "coder_code_anim"])
    def test_all_placeholders_resolve(self, name: str) -> None:
        """渲染后不得残留 ``{{...}}`` —— 残留会被模型原样抄进代码。"""
        rendered = render_engine_prompt(name)
        leftover = _UNRESOLVED.findall(rendered)
        assert not leftover, f"{name} 残留未解析的占位符：{leftover}"

    @pytest.mark.parametrize("name", ["coder_manim", "coder_html", "coder_code_anim"])
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
        for name in ("coder_manim", "coder_html", "coder_code_anim"):
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

    def test_truncates_long_code(self) -> None:
        example = FewShot(id="a", title="t", tag="MATH", engine="manim",
                          summary="s", code="x" * 5000)
        assert len(render_examples([example])) < 3000


class TestCodeArtifact:
    def test_defaults(self) -> None:
        artifact = CodeArtifact()
        assert artifact.code == "" and artifact.language == "python"
        assert artifact.explanation == ""
