"""编码智能体：把分镜的视觉意图翻译成某个渲染引擎能执行的代码。

职责边界很明确：
* 决定**用哪个提示词**（按标签路由到引擎，见 ``PROMPT_BY_ENGINE``）；
* 注入 **RAG 召回的 Few-shot 范例**（把"自由发挥"变成"模仿已验证的范式"）；
* 注入**上一轮的意见**（VLM 审查意见或人类打回意见），实现修正式重写；
* 产出后立刻做**静态安全检查**，把危险/非法代码挡在渲染之前。

为什么静态检查要放在这里而不是渲染器里：静态检查是**毫秒级**的，
而一次渲染是**数十秒**的。在这里拦住一个 `import os` 或一段缺少
``window.__seek`` 的 HTML，省下的是整整一轮渲染 + 一次 VLM 调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..llm import LLMClient, LLMError, LLMParseError, Task
from ..logging import get_logger
from ..rag import FewShot, FewShotRetriever, build_retriever
from ..renderer import LLM_ENGINES, check_html_contract
from ..sandbox.policy import PolicyReport, PolicyViolation, check_source
from ..schemas import RenderEngine, ShotSpec, StyleGuide
from .base import Agent, load_prompt, render_prompt, style_guide_to_text

logger = get_logger(__name__)

#: 引擎 -> 系统提示词文件名。**确定性映射**，模型无权选择提示词。
PROMPT_BY_ENGINE: dict[str, str] = {
    RenderEngine.MANIM.value: "coder_manim",
    RenderEngine.D3.value: "coder_html",
    RenderEngine.ECHARTS.value: "coder_html",
    RenderEngine.CODE_ANIM.value: "coder_code_anim",
}

#: 系统提示词里会被注入的占位符。
#: 供契约测试与 ``load_engine_prompt`` 保持一致 —— 任何新增占位符
#: 都必须在这里登记，否则渲染后会残留 ``{{...}}`` 被模型照抄进输出。
ENGINE_PROMPT_PLACEHOLDERS: tuple[str, ...] = (
    "min_font_size",
    "background_color",
    "primary_color",
    "width",
    "height",
    "fps",
    "duration_sec",
)

#: 走 HTML 逐帧截图路径的引擎（静态检查项与 Manim 不同）。
_HTML_ENGINES: frozenset[str] = frozenset({"d3", "echarts", "code_anim"})

#: 静态检查失败后允许的自动修复次数。
#: 设为 1 而不是更多：把违规回灌给模型通常一次就能改对；
#: 再改不对说明是系统性问题（提示词或模型能力），继续烧 token 不划算。
MAX_POLICY_REPAIRS = 1


class CodeArtifact(BaseModel):
    """模型产出的渲染代码。"""

    code: str = ""
    language: str = "python"
    explanation: str = ""


class _RawCode(BaseModel):
    """模型直出的结构（字段刻意与 CodeArtifact 一致，便于契约测试比对）。"""

    code: str = ""
    language: str = "python"
    explanation: str = Field(default="")


@dataclass
class CodeGenerationResult:
    """一次代码生成的结果（含静态检查结论与成本信息）。"""

    artifact: CodeArtifact
    policy_ok: bool = True
    policy_summary: str = ""
    llm_attempts: int = 1
    examples_used: list[str] = field(default_factory=list)
    #: 是否跳过了模型调用（氛围镜头由 ffmpeg 程序化生成）。
    skipped_llm: bool = False
    #: 供上层构造事件/日志的标题（氛围镜头用）。
    overlay_text: str = ""

    @property
    def code(self) -> str:
        return self.artifact.code

    @property
    def engine_hint(self) -> str:
        return self.artifact.language


class CoderAgent(Agent):
    """编码智能体。"""

    prompt_name = "coder_user"

    def __init__(
        self,
        llm: LLMClient | None = None,
        settings: Settings | None = None,
        retriever: FewShotRetriever | None = None,
    ) -> None:
        super().__init__(llm)
        self.settings = settings or get_settings()
        self.retriever = retriever if retriever is not None else build_retriever(self.settings)

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def generate(
        self,
        *,
        shot: ShotSpec,
        style_guide: StyleGuide,
        attempt: int = 1,
        feedback_text: str = "",
        previous_code: str = "",
    ) -> CodeGenerationResult:
        """为分镜生成渲染代码。

        ``feedback_text`` 同时承接三类来源（VLM 意见、人类打回、渲染报错），
        统一走同一条回灌通道 —— 编码侧无需区分来源，链路因此简单可靠。
        """
        engine = shot.engine.value if shot.engine else ""

        # 氛围镜头不需要模型：直接由 ffmpeg 程序化生成，省一次调用。
        if engine not in LLM_ENGINES:
            return self._programmatic_ambient(shot, engine)

        examples = self.retriever.retrieve(shot, k=3)
        user_prompt = self._build_user_prompt(
            shot=shot,
            style_guide=style_guide,
            attempt=attempt,
            feedback_text=feedback_text,
            previous_code=previous_code,
            examples=examples,
        )

        system_prompt = load_engine_prompt(
            PROMPT_BY_ENGINE[engine],
            self.settings,
            min_font_size=style_guide.min_font_size,
            duration_sec=shot.duration_sec,
            background_color=style_guide.background_color,
            primary_color=style_guide.primary_color,
        )

        artifact = self._call_model(engine, system_prompt, user_prompt)
        report = self._check(artifact, engine)

        result = CodeGenerationResult(
            artifact=artifact,
            policy_ok=report.ok,
            policy_summary=report.summary(),
            examples_used=[ex.id for ex in examples],
        )

        # 静态检查失败 -> 把违规信息回灌给模型修复一次。
        if not result.policy_ok:
            logger.warning(
                "生成的代码未通过静态检查，尝试自动修复",
                extra={
                    "shot_id": shot.shot_id,
                    "engine": engine,
                    "violations": len(report.errors),
                    "summary": result.policy_summary[:300],
                },
            )
            repaired = self._repair(
                shot, style_guide, artifact, result.policy_summary, examples, system_prompt
            )
            if repaired is not None:
                result.artifact = repaired
                report2 = self._check(repaired, engine)
                result.policy_ok = report2.ok
                result.policy_summary = report2.summary()
                result.llm_attempts += 1

        if not result.policy_ok:
            logger.warning(
                "代码在自动修复后仍未通过静态检查，交由流水线决定是否重试",
                extra={
                    "shot_id": shot.shot_id,
                    "engine": engine,
                    "summary": result.policy_summary[:300],
                },
            )
        else:
            logger.info(
                "code 生成完成",
                extra={
                    "shot_id": shot.shot_id,
                    "engine": engine,
                    "attempt": attempt,
                    "code_bytes": len(result.code),
                    "examples": result.examples_used,
                    "llm_attempts": result.llm_attempts,
                },
            )
        return result

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _programmatic_ambient(self, shot: ShotSpec, engine: str) -> CodeGenerationResult:
        """氛围镜头：无需模型，直接给渲染器空代码 + 一个标题。

        标题取画外音的首句并截断 —— 观众看到的是一个标题卡。
        截断到 20 字是经验值：再长在小屏上会换行到第三行，破坏构图。
        """
        overlay = shot.meta.get("overlay_text") or title_from(shot.narration)
        logger.debug(
            "氛围镜头跳过 LLM，由 ffmpeg 程序化渲染",
            extra={"shot_id": shot.shot_id, "engine": engine or "stock", "overlay": overlay},
        )
        return CodeGenerationResult(
            artifact=CodeArtifact(
                code="",
                language="none",
                explanation=f"程序化渐变背景（标题：{overlay or '无'}）",
            ),
            skipped_llm=True,
            overlay_text=overlay,
        )

    def _call_model(self, engine: str, system_prompt: str, user_prompt: str) -> CodeArtifact:
        try:
            parsed = self.llm.chat_json(
                system_prompt,
                user_prompt,
                _RawCode,
                model=self.settings.llm_model,
                # 显式声明任务类型：mock 模式据此返回确定的结构，
                # 而不是从提示词里嗅探关键词（那曾导致返回错误结构并被静默解析）。
                task=Task.CODE,
            )
        except (LLMParseError, LLMError) as exc:
            raise LLMError(f"编码智能体生成失败（{engine}）：{exc}") from exc

        assert isinstance(parsed, _RawCode)
        if not parsed.code.strip():
            raise LLMError(f"编码智能体返回了空代码（{engine}）")
        return CodeArtifact(
            code=parsed.code, language=parsed.language, explanation=parsed.explanation
        )

    def _repair(
        self,
        shot: ShotSpec,
        style_guide: StyleGuide,
        artifact: CodeArtifact,
        violations: str,
        examples: list[FewShot],
        system_prompt: str,
    ) -> CodeArtifact | None:
        """把静态检查的违规信息回灌，请模型修正。

        比"直接重生成"更有效：模型能看到自己具体错在哪一行、错在什么，
        而不是重新盲写一遍（很可能再犯同样的错）。
        """
        engine = shot.engine.value if shot.engine else ""
        feedback = (
            "【静态安全检查未通过，请修正下列问题后重新输出完整代码】\n"
            f"{violations}\n\n"
            "【上一版代码】\n"
            f"```\n{artifact.code[:6000]}\n```"
        )
        user_prompt = self._build_user_prompt(
            shot=shot,
            style_guide=style_guide,
            attempt=2,
            feedback_text=feedback,
            previous_code=artifact.code,
            examples=examples,
        )
        try:
            return self._call_model(engine, system_prompt, user_prompt)
        except LLMError as exc:
            logger.warning("静态检查后的自动修复调用失败", extra={"error": str(exc)[:300]})
            return None

    def _check(self, artifact: CodeArtifact, engine: str) -> PolicyReport:
        """对产物做静态安全检查。

        Manim 分支走 AST 白名单；HTML 分支走**渲染契约**检查
        （必须有 ``window.__seek``、不得引 CDN）。
        检查项不同但结论同构（ok / 违规摘要），因此调用方无需分支。
        """
        if engine in _HTML_ENGINES:
            return check_html_contract(artifact.code)
        return check_source(artifact.code)

    def _build_user_prompt(
        self,
        *,
        shot: ShotSpec,
        style_guide: StyleGuide,
        attempt: int,
        feedback_text: str,
        previous_code: str,
        examples: list[FewShot],
    ) -> str:
        examples_text = render_examples(examples)
        feedback_block = ""
        if feedback_text.strip():
            # 明确区分"审查意见"与"技术错误"：模型对两类信息的处理方式不同，
            # 前者要求改画面，后者要求改代码结构。
            header = "上一轮反馈（必须逐条处理）" if attempt > 1 else "反馈（必须逐条处理）"
            feedback_block = f"## {header}\n\n{feedback_text.strip()}\n"
            if previous_code:
                feedback_block += (
                    "\n**上一版代码（供参考，不要原样输出）**\n"
                    f"```\n{previous_code[:6000]}\n```\n"
                )

        return render_prompt(
            "coder_user",
            index=shot.index,
            tag=shot.tag.value,
            engine=shot.engine.value if shot.engine else "",
            duration_sec=round(shot.duration_sec, 2),
            narration=shot.narration or "（无画外音）",
            visual_brief=shot.visual_brief or "（无明确视觉意图，请按标签给出合理画面）",
            keywords="、".join(shot.keywords) or "（无）",
            style_guide=style_guide_to_text(style_guide),
            examples=examples_text,
            feedback_block=feedback_block,
        )


# ---------------------------------------------------------------------------
# 提示词与工具
# ---------------------------------------------------------------------------

_prompt_cache: dict[str, str] = {}


def load_engine_prompt(
    name: str,
    settings: Settings,
    *,
    min_font_size: int,
    duration_sec: float,
    background_color: str,
    primary_color: str,
) -> str:
    """装载引擎专属系统提示词，并注入**真实**渲染参数。

    与 :func:`agents.base.load_prompt` 的区别：这里要做参数注入。
    把硬约束（字号下限、时长、配色）同时写进系统提示与用户提示，
    能显著提高模型对它的服从度 —— 只写在用户消息里时，模型很容易忽略。

    注入真实值而不是占位提示（例如把 ``{{duration_sec}}`` 换成 ``（见用户消息）``）：
    提示词里的代码范例应当是**可直接套用**的，含糊的占位符会让模型
    把示例当成伪代码。
    """
    template = _prompt_cache.get(name) or load_prompt(name)
    _prompt_cache[name] = template

    mapping = {
        "min_font_size": str(min_font_size),
        "background_color": background_color,
        "primary_color": primary_color,
        "width": str(settings.render_width),
        "height": str(settings.render_height),
        "fps": str(settings.render_fps),
        "duration_sec": f"{duration_sec:g}",
    }
    rendered = template
    for key, value in mapping.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def render_examples(examples: list[FewShot]) -> str:
    """把召回的范例渲染成提示词片段。"""
    if not examples:
        return "（本次没有召回到参考范例，请按系统提示里的范式自行实现）"
    blocks: list[str] = []
    for ex in examples:
        block = f"### 范例：{ex.title}（标签 {ex.tag}）\n要点：{ex.summary}"
        if ex.code.strip():
            block += f"\n```\n{ex.code[:2500]}\n```"
        blocks.append(block)
    return "\n\n".join(blocks)


#: 标题的分句标点（按"语义强度"排列，但切分时取**文本中最先出现**的那个）。
_TITLE_SEPARATORS: tuple[str, ...] = (
    "。", "！", "？", "；", "，", ".", "!", "?", ";", ",", "\n",
)


def title_from(narration: str, limit: int = 20) -> str:
    """从画外音里截出一个适合做标题的短句。

    按中文标点取**首句** —— 直接硬截断会把词切断（"相对论的基本假设是"），
    读起来像 bug。

    注意必须比较**在文本中的位置**，而不是分隔符的遍历顺序：
    ``第一句！第二句。`` 里 ``。`` 排在遍历表前面，但 ``！`` 出现得更早，
    按遍历顺序切会得到 ``第一句！第二句`` —— 把第二句也带进来了。
    """
    text = (narration or "").strip()
    if not text:
        return ""

    positions = [pos for sep in _TITLE_SEPARATORS if (pos := text.find(sep)) > 0]
    if positions:
        text = text[: min(positions)].strip()

    return text if len(text) <= limit else text[: limit - 1] + "…"


__all__ = [
    "ENGINE_PROMPT_PLACEHOLDERS",
    "MAX_POLICY_REPAIRS",
    "PROMPT_BY_ENGINE",
    "CodeArtifact",
    "CodeGenerationResult",
    "CoderAgent",
    "load_engine_prompt",
    "render_examples",
    "title_from",
    "PolicyViolation",  # 便于调用方构造/判断违规
]
