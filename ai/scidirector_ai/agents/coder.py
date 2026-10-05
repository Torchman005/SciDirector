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
from ..generation_quality import BrowserPreflight, QualityChecker, QualityResult, check_manim_structure, production_brief
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
    # 通用二维动效：界面演示、图标/角色动画、示意图。
    # 与 d3/echarts 共用渲染机制，但提示词不同 —— 那两套是"画数据图"的专用模板，
    # 拿它们画聊天界面只会画出奇怪的东西。
    RenderEngine.MOTION.value: "coder_motion",
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
_HTML_ENGINES: frozenset[str] = frozenset({"d3", "echarts", "code_anim", "motion"})

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
    quality: QualityResult = field(default_factory=QualityResult)

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
        quality_checker: QualityChecker | None = None,
    ) -> None:
        super().__init__(llm)
        self.settings = settings or get_settings()
        self.retriever = retriever if retriever is not None else build_retriever(self.settings)
        self.quality_checker: QualityChecker = quality_checker if quality_checker is not None else BrowserPreflight(self.settings)

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

        # few-shot 条数可配，**默认 2 而不是原来的硬编码 3**。
        #
        # 这是 token 账上最直接的一刀：语料里每条示例约 850 token，而这段
        # 会被**每次渲染**注入（7 镜头 × 平均 2 次尝试 = 14 次）。3 -> 2
        # 每次省约 850 token，一轮下来约 12k。
        #
        # 降到 1 太狠：示例承担的是"输出格式与代码风格"的锚定，
        # 只剩一条时模型容易退回自己习惯的写法（本项目已在别处吃过
        # "示例锚定"的亏）。所以默认留在 2，需要更省再往下降。
        k = self.settings.rag_few_shot_k
        examples = [ex for ex in self.retriever.retrieve(shot, k=k) if ex.engine == engine]
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
        quality = self.quality_checker.check(artifact.code, shot, style_guide) if report.ok else QualityResult(reason="静态检查未通过，未执行预检")

        result = CodeGenerationResult(
            artifact=artifact,
            policy_ok=report.ok and not quality.issues,
            policy_summary=report.summary() if not report.ok else "；".join(quality.issues) or report.summary(),
            quality=quality,
            examples_used=[ex.id for ex in examples],
        )

        # Safety and visual preflight share one repair budget; never multiply model loops.
        if not result.policy_ok:
            logger.warning(
                "生成代码未通过安全/质量检查，尝试生成内修复",
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
            result.llm_attempts += 1
            if repaired is not None:
                result.artifact = repaired
                report2 = self._check(repaired, engine)
                result.quality = (self.quality_checker.check(repaired.code, shot, style_guide) if report2.ok
                                  else QualityResult(reason="静态检查未通过，未执行预检"))
                result.policy_ok = report2.ok and not result.quality.issues
                result.policy_summary = (report2.summary() if not report2.ok
                                         else "；".join(result.quality.issues) or report2.summary())

        if not result.policy_ok:
            logger.warning(
                "代码在生成内修复后仍未通过安全/质量检查，交由流水线决定是否重试",
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
                    "quality_checked": result.quality.checked,
                    "quality_reason": result.quality.reason,
                    "quality_warnings": result.quality.warnings,
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
            code=parsed.code, language="python" if engine == "manim" else "html+js",
            explanation=parsed.explanation
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
        # 这里**只放违规信息**，不再重复内联一份上一版代码。
        #
        # 原实现把 `artifact.code[:6000]` 塞进 feedback（截断），同时又把
        # `previous_code=artifact.code` 传下去（在 `_build_user_prompt` 里再截断一次），
        # 于是提示词里出现**两份残缺的**上一版代码 —— 既浪费上下文，
        # 又让"请重新输出完整代码"这句话与它看到的残缺内容自相矛盾。
        # 代码由 `_build_user_prompt` 统一注入，只此一处、且会显式标注截断。
        feedback = (
            "【静态安全检查未通过或首稿质量预检未通过，请按实际错误修正后重新输出完整代码】\n"
            f"{violations}"
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
        report = check_source(artifact.code)
        if report.ok:
            report.violations.extend(check_manim_structure(artifact.code).violations)
        return report

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
                    "\n**上一版代码（以此为基础修改）**\n"
                    "保留已正确的部分，逐条落实反馈。若反馈指出画面或节奏没有改善，"
                    "必须改变对应元素或阶段的可见状态；只改注释、变量名或微调无关参数不算修复。"
                    "输出完整的新代码，不要原样返回上一版。\n"
                    f"```\n{_clip_code_for_prompt(previous_code)}\n```\n"
                )

        quality_brief = render_prompt("coder_quality", production_brief=production_brief(shot, style_guide, self.settings))
        return render_prompt(
            "coder_user",
            index=shot.index,
            tag=shot.tag.value,
            engine=shot.engine.value if shot.engine else "",
            duration_sec=round(shot.duration_sec, 2),
            narration=shot.narration or "（无画外音）",
            visual_brief=shot.visual_brief or "（无明确视觉意图，请按标签给出合理画面）",
            keywords="、".join(shot.keywords) or "（无）",
            beats=" → ".join(shot.beats) or "（无；请自行安排清晰的起承转合）",
            style_guide=style_guide_to_text(style_guide),
            examples=examples_text,
            feedback_block=feedback_block + "\n\n" + quality_brief,
        )


# ---------------------------------------------------------------------------
# 提示词与工具
# ---------------------------------------------------------------------------

_prompt_cache: dict[str, str] = {}

#: 上一版代码注入提示词时的长度上限。
#:
#: 定这个值之前踩过一次代价很大的坑：原值是 **6000**，而实测生成的 HTML 动效代码
#: 最长到 **6681** 字符 —— 12 个镜头里有 4 个（约三分之一）在"重做 / 修复"时，
#: 模型看到的上一版是**残缺的**；提示词里却写着"不要原样输出""重新输出完整代码"，
#: 于是模型很可能把没看到的那一段直接丢掉 —— 而那一端往往正是收尾的
#: `window.__seek` 与闭合标签，丢了就是渲染契约失败或画面坏掉。
#:
#: 最糟的是**这一切没有任何日志**：截断是静默的，只表现为"打回重做效果不好"。
#:
#: 现在取 24000（实测最大值的 3.5 倍），足以让真实产物**原样**进提示词；
#: 真超了也不再静默截断，而是头尾各留一段并**显式标注**中间省略了多少字符。
_MAX_PREVIOUS_CODE_CHARS = 24000


def _clip_code_for_prompt(code: str, *, what: str = "上一版代码") -> str:
    """把要注入提示词的代码裁到上限内；**绝不静默丢内容**。

    低于上限时原样返回（绝大多数情况）。超过时保留**头与尾**、中间显式标注省略量：
    头有结构与样式，尾有渲染契约（`window.__seek`）与闭合标签 ——
    两端都比中间那段更容易被模型当成"可以省掉"的东西，所以都不能丢。
    """
    if len(code) <= _MAX_PREVIOUS_CODE_CHARS:
        return code
    head_len = int(_MAX_PREVIOUS_CODE_CHARS * 0.6)
    tail_len = _MAX_PREVIOUS_CODE_CHARS - head_len
    omitted = len(code) - _MAX_PREVIOUS_CODE_CHARS
    return (
        code[:head_len]
        + f"\n\n# …（{what}过长，此处省略了 {omitted} 个字符，只保留开头与结尾）\n"
        "# 你必须输出**完整且自洽**的文件；不要因为看不到中间，\n"
        "# 就删掉结尾的渲染契约（window.__seek）或任何闭合标签。\n\n"
        + code[-tail_len:]
    )


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
    """把召回的范例渲染成提示词片段。

    范例代码同样走 :func:`_clip_code_for_prompt`，**不再静默截断**。
    原先这里是 ``ex.code[:2500]``，而语料里 7 条范例有 2 条超过 2500 字符
    （最长 3899）—— 也就是说模型有时会拿到一份**残缺的范例**当作"参考写法"，
    而残缺的范例比没有范例更糟：它可能照着学，把缺掉收尾（`window.__seek`、
    闭合标签）的写法一起学过去。
    """
    if not examples:
        return "（本次没有召回到参考范例，请按系统提示里的范式自行实现）"
    blocks: list[str] = []
    for ex in examples:
        block = f"### 范例：{ex.title}（标签 {ex.tag}）\n要点：{ex.summary}"
        if ex.code.strip():
            block += f"\n```\n{_clip_code_for_prompt(ex.code, what='范例代码')}\n```"
        blocks.append(block)
    return "\n\n".join(blocks)


#: 标题的分句标点（按"语义强度"排列，但切分时取**文本中最先出现**的那个）。
#: 冒号必须在内：`勾股定理：直角三角形……` 的标题就该是 `勾股定理`。
#: 少了它会截成 `勾股定理：直角三角形两条直角…` —— 又长又断在半句上，
#: VLM 审查会如实记成"文字被截断"，而这确实是个观感缺陷。
_TITLE_SEPARATORS: tuple[str, ...] = (
    "。", "！", "？", "；", "，", "：",
    ".", "!", "?", ";", ",", ":", "\n",
)


def title_from(narration: str, limit: int = 20) -> str:
    """从画外音里截出一个适合做标题的短句。

    按中文标点取**首句** —— 直接硬截断会把词切断（"相对论的基本假设是"），
    读起来像 bug。冒号同样算分句：``勾股定理：直角三角形……`` 的标题应当就是
    ``勾股定理``，否则会变成 ``勾股定理：直角三角形两条直角…``，
    又长又断在半句上。

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

    if len(text) <= limit:
        return text

    # 能走到这里，说明**首句本身就超过 limit**：limit 之内不可能再有分句标点 ——
    # 否则上面的首句切分早就把 text 切短、在那里就返回了。
    # 所以这里没有"更聪明的退让"可选，只能硬截断。
    return text[: limit - 1].rstrip() + "…"


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
