"""领域模型（Pydantic v2）。

与 ``proto/scidirector/v1`` 的关系：
* proto 是**跨语言边界**的契约（强类型、有兼容规则）；
* 本模块是 **Python 进程内部**的领域模型，负责业务校验、默认值与派生逻辑。

两者由 ``grpc_server`` 中的转换函数显式对齐。之所以不直接复用生成的 pb 类：
* pb 类是「哑数据结构」，没有校验，无法表达「分镜时长之和必须接近目标时长」这类业务约束；
* 提示词、RAG、沙盒等内部流程需要更友好的类型（如 ``Path``、枚举、元组）。

**禁止**在 pb 与本模块之间做隐式 duck typing —— 必须显式转换，
这样契约演进时编译/类型检查能第一时间报错。
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ===========================================================================
# 枚举
# ===========================================================================


class SceneTag(str, Enum):
    """场景标签：由导演智能体打标，决定后续渲染路由。

    标签错了，后面全错 —— 这是整套系统的枢纽。
    """

    MATH = "MATH"          # [数学] 公式推导、几何演示 -> Manim
    DATA = "DATA"          # [数据] 统计图表、趋势对比 -> D3 / ECharts
    CODE = "CODE"          # [代码] 算法讲解、代码演示 -> 代码高亮动画
    AMBIENCE = "AMBIENCE"  # [氛围] 过渡、情绪铺陈 -> HTML 动态背景 + 一行标题
    MOTION = "MOTION"      # [动效] 界面演示、图标/角色动画、示意图 -> HTML/CSS/JS

    @classmethod
    def from_prompt(cls, raw: str) -> SceneTag:
        """把模型输出的自由文本标签归一化。

        模型经常会输出 ``[数学]`` / ``math`` / ``MATH`` 等变体，
        统一在这里归一，避免调用点各写一遍兼容逻辑。

        兜底仍是 ``AMBIENCE``：它没有具体对象、最容易画，而且**浏览器不可用时
        还会进一步降级回 ffmpeg 渐变**（见 ``graph/nodes.py::_engine_or_fallback``），
        因此任何环境下都画得出来。
        """
        text = (raw or "").strip().strip("[]【】").upper()
        aliases = {
            "数学": cls.MATH, "公式": cls.MATH, "MATH": cls.MATH, "MATHSCENE": cls.MATH,
            "数据": cls.DATA, "图表": cls.DATA, "DATA": cls.DATA, "CHART": cls.DATA,
            "代码": cls.CODE, "编程": cls.CODE, "CODE": cls.CODE, "CODING": cls.CODE,
            # 动效：产品演示、界面讲解、图标/角色动画、示意图。
            # 这些内容光靠 MATH / DATA / CODE 都画不出来（MATH 只画公式、
            # DATA 只画图表、CODE 只画代码，而 AMBIENCE 只画背景与标题、
            # 没有具体对象），所以必须让模型有一个**明确的去处**，
            # 否则它会写成 AMBIENCE 再描述一堆画不出来的画面。
            "动效": cls.MOTION, "动画": cls.MOTION, "界面": cls.MOTION,
            "演示": cls.MOTION, "示意": cls.MOTION, "图形": cls.MOTION,
            "图标": cls.MOTION, "场景": cls.MOTION,
            "MOTION": cls.MOTION, "MOTIONGRAPHICS": cls.MOTION,
            "ANIMATION": cls.MOTION, "UI": cls.MOTION, "UX": cls.MOTION,
            "GRAPHIC": cls.MOTION, "GRAPHICS": cls.MOTION,
            "DIAGRAM": cls.MOTION, "ILLUSTRATION": cls.MOTION,
            "氛围": cls.AMBIENCE, "过渡": cls.AMBIENCE, "AMBIENCE": cls.AMBIENCE,
            "MOOD": cls.AMBIENCE, "TRANSITION": cls.AMBIENCE,
        }
        return aliases.get(text, cls.AMBIENCE)


class RenderEngine(str, Enum):
    """渲染引擎。由标签**确定性映射**得到，模型不得自由指定。"""

    MANIM = "manim"
    D3 = "d3"
    ECHARTS = "echarts"
    CODE_ANIM = "code_anim"
    STOCK = "stock"
    #: HTML/CSS/JS 任意二维动效。与 d3/echarts/code_anim 走**同一条**浏览器通路，
    #: 区别只在提示词与用途：那三个是"数据/代码"的专用模板，这个是通用的。
    MOTION = "motion"


# 标签 -> 引擎的确定性路由表。
# 与 Go 侧 ``domain.EngineForTag`` 必须保持一致（见 Agent.md §5.2）。
TAG_TO_ENGINE: dict[SceneTag, RenderEngine] = {
    SceneTag.MATH: RenderEngine.MANIM,
    SceneTag.DATA: RenderEngine.D3,
    SceneTag.CODE: RenderEngine.CODE_ANIM,
    # AMBIENCE 也走 HTML 动画，**不再**用 ffmpeg 固定渐变。
    #
    # 为什么改：渐变渲染器的配色与流速是写死的，于是**每个环境镜头都长得一模一样**，
    # 整片看下来就是"同一张背景在换文字"—— 这正是用户的原话。而这类镜头恰恰数量
    # 不少（开场、过渡、金句收尾）。交给 HTML 之后，每个镜头由模型各写一个场景，
    # 光晕、粒子、文字动效都能不一样，成本只多一次模型调用 + 一次浏览器渲染。
    #
    # 注意 `stock` 引擎**没有删除**：浏览器不可用时由图节点把它降级回去
    # （见 ``graph/nodes.py`` 的 `_engine_or_fallback`）—— "环境镜头永远画得出来"
    # 是它作为兜底的唯一价值，不能因为换了默认实现就丢掉。
    SceneTag.AMBIENCE: RenderEngine.MOTION,
    SceneTag.MOTION: RenderEngine.MOTION,
}


class FeedbackSource(str, Enum):
    """审查意见来源。VLM 与人类共用同一结构，简化回灌链路。"""

    VLM = "VLM"
    HUMAN = "HUMAN"
    SYSTEM = "SYSTEM"


# ===========================================================================
# 核心数据结构
# ===========================================================================


class ShotSpec(BaseModel):
    """一个分镜规格。既是渲染指令，也是审查对象。"""

    model_config = ConfigDict(use_enum_values=False)

    shot_id: str = Field(default="", description="稳定 ID；为空时由 job_id + index 派生")
    index: int = Field(default=0, ge=0, description="全片序号，从 0 开始")
    narration: str = Field(default="", description="画外音 / 字幕文本")
    visual_brief: str = Field(default="", description="视觉意图的自然语言描述")
    tag: SceneTag = Field(default=SceneTag.AMBIENCE, description="场景标签（路由依据）")
    engine: RenderEngine | None = Field(default=None, description="目标渲染引擎；留空则按标签推导")
    duration_sec: float = Field(default=5.0, gt=0, le=600, description="目标时长（秒）")
    keywords: list[str] = Field(default_factory=list, description="检索关键词")
    #: 画面内容的顺序节拍，不带绝对时间，避免后续时长修复后失真。
    beats: list[str] = Field(default_factory=list, description="画面阶段的顺序描述")
    code: str = Field(default="", description="当前版本的渲染源码")
    language: str = Field(default="", description="源码语言：python / html+js")
    meta: dict[str, str] = Field(default_factory=dict, description="扩展位，禁止放二进制")

    @model_validator(mode="after")
    def _apply_engine_routing(self) -> ShotSpec:
        """引擎留空时按标签推导；显式指定的引擎不被覆盖（便于人工干预）。"""
        if self.engine is None:
            self.engine = TAG_TO_ENGINE[self.tag]
        return self

    @field_validator("keywords", mode="before")
    @classmethod
    def _coerce_keywords(cls, v: Any) -> list[str]:
        """模型经常把关键词写成逗号分隔的字符串，这里做兼容。"""
        if v is None:
            return []
        if isinstance(v, str):
            return [k.strip() for k in re.split(r"[,，、;；]", v) if k.strip()]
        return list(v)

    @field_validator("duration_sec", mode="before")
    @classmethod
    def _coerce_duration(cls, v: Any) -> float:
        """兼容模型输出 ``"5s"`` / ``"5 秒"`` 这类带单位的字符串。"""
        if isinstance(v, str):
            m = re.search(r"-?\d+(?:\.\d+)?", v)
            if m:
                return float(m.group())
        return v

    @property
    def stable_id(self) -> str:
        """派生稳定 ID（与 Go 侧 ``domain.ShotID`` 规则一致）。"""
        if self.shot_id:
            return self.shot_id
        return f"{self.index:03d}"


class RepairTask(BaseModel):
    """问题定位与验收目标；task_id 由程序分配，复审不得改写验收条件。"""

    task_id: str = ""
    category: Literal["logic", "readability", "pacing", "layout", "rendering"] = "layout"
    severity: Literal["blocking", "major", "advisory"] = "major"
    start_sec: float = Field(default=0.0, ge=0.0)
    end_sec: float = Field(default=0.0, ge=0.0)
    frame_indices: list[int] = Field(default_factory=list)
    target: str = ""
    evidence: str = ""
    instruction: str = ""
    acceptance: str = ""
    region: list[float] = Field(default_factory=list)
    status: Literal["open", "partial", "resolved", "unverified"] = "open"
    resolution_evidence: str = ""

    @model_validator(mode="after")
    def _validate_location(self) -> RepairTask:
        if self.end_sec < self.start_sec or any(i < 1 for i in self.frame_indices):
            raise ValueError("修复任务时间段或帧号无效")
        if self.region:
            if len(self.region) != 4:
                raise ValueError("region 必须为 x/y/width/height")
            x, y, width, height = self.region
            if min(x, y) < 0 or min(width, height) <= 0 or x + width > 1 or y + height > 1:
                raise ValueError("region 必须位于归一化画面范围内")
        return self

    @property
    def actionable(self) -> bool:
        return (all(value.strip() for value in
                    (self.target, self.evidence, self.instruction, self.acceptance))
                and (bool(self.frame_indices) or self.end_sec > self.start_sec))


class CriticFeedback(BaseModel):
    """审查意见（VLM 或人类）。"""

    passed: bool = False
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    issues: list[str] = Field(default_factory=list, description="发现的问题")
    suggestions: list[str] = Field(
        default_factory=list,
        description="【必须可执行】例如「字号 24 -> 48」；禁止「画面不好看」这类不可执行意见",
    )
    fatal_issues: list[str] = Field(default_factory=list, description="可核对的致命问题")
    repair_tasks: list[RepairTask] = Field(default_factory=list)
    raw_response: str = ""
    model: str = ""
    source: FeedbackSource = FeedbackSource.VLM
    attempt: int = 0
    # 分维度得分：便于统计「问题主要集中在可读性还是节奏」。
    logic_score: float = Field(default=0.0, ge=0.0, le=1.0)
    readability_score: float = Field(default=0.0, ge=0.0, le=1.0)
    pacing_score: float = Field(default=0.0, ge=0.0, le=1.0)
    aesthetics_score: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _enforce_actionable_feedback(self) -> CriticFeedback:
        """不通过时必须至少给一条可执行建议。

        这是**契约级约束**而不是「提示词建议」：没有可执行建议的反馈
        无法转换为代码修改，回灌给编码智能体只会得到同样的画面，
        白白烧掉一次渲染 + 一次 VLM 调用。因此在数据层就拦住。
        """
        if not self.passed and not self.suggestions:
            raise ValueError(
                "审查未通过时必须给出至少一条可执行的修改建议（suggestions 不能为空）"
            )
        return self

    @property
    def actionable(self) -> bool:
        """是否具备可执行的修改指令（供上层决定能不能自动重试）。"""
        return bool(self.suggestions)


class RenderArtifact(BaseModel):
    """渲染产物。只存路径与元数据，绝不内嵌二进制。"""

    artifact_id: str = ""
    shot_id: str = ""
    video_path: str = ""
    audio_path: str = ""
    subtitle_path: str = ""
    duration_sec: float = 0.0
    width: int = 0
    height: int = 0
    fps: int = 0
    attempt: int = 0
    engine: str = ""
    frame_samples: list[str] = Field(default_factory=list)
    rendered_at_unix_ms: int = 0
    render_cost_sec: float = 0.0


class ScriptPlan(BaseModel):
    """导演智能体的完整产出。"""

    outline: str = Field(default="", description="全片叙事大纲，便于人工审核与调试")
    shots: list[ShotSpec] = Field(default_factory=list)
    total_tokens: int = 0


#: 内置风格预设。
#:
#: 分工要清楚：**预设管生成期的配色**（会被注入各引擎的提示词，决定模型怎么写画面）；
#: **后期的调色是 effects.grade**（Go 侧 ffmpeg 滤镜，见 media/effects.go）。
#: 两者刻意分开：一个影响"画什么颜色"，一个影响"整片统一成什么色调"，
#: 混在一起会让"这份配置到底作用在哪一段"不可推理。
#:
#: 新增预设时请同时更新 docs/API.md 的清单 —— 前端下拉框正是照它写的。
STYLE_PRESETS: dict[str, dict[str, str]] = {
    "default": {"primary_color": "#4F8CFF", "background_color": "#0B1020"},
    "tech": {"primary_color": "#3DDC97", "background_color": "#08111F"},
    "warm": {"primary_color": "#FF8A4C", "background_color": "#1A1013"},
    "minimal": {"primary_color": "#E6ECFF", "background_color": "#101216"},
    "nature": {"primary_color": "#5FD68A", "background_color": "#0B1A12"},
    "sunset": {"primary_color": "#FF6B9D", "background_color": "#1B1020"},
}

DEFAULT_PRESET = "default"


class StyleGuide(BaseModel):
    """风格约束。跨镜头一致性靠它维持（避免每个镜头风格漂移）。"""

    theme: str = Field(default="dark", description="dark / light")
    #: 预设名。显式填了 primary_color / background_color 时以显式值为准。
    preset: str = Field(default=DEFAULT_PRESET, description="风格预设，见 STYLE_PRESETS")
    #: 背景样式（见 backgrounds.BACKGROUND_STYLE_IDS）。
    #: 缺省 auto = "由模型按内容决定"，与既有任务行为一致。
    background_style: str = Field(default="auto", description="背景样式，见 BACKGROUND_STYLE_IDS")
    # 空串 = **未指定**，由 preset 填充。
    #
    # 用空串而不是 None 表示"没填"：这两个字段最终一定会被填成具体色值，
    # 保持 `str` 类型可以让所有下游（提示词注入、pbconv）不必到处判 None。
    primary_color: str = ""
    background_color: str = ""
    font_family: str = "Noto Sans CJK SC"
    # 正文字号下限：直接对应审查 rubric 中的「文字可读性」，
    # 也是模型最常犯的错误（字号过小）。
    min_font_size: int = Field(default=32, ge=12, le=200)
    aspect_ratio: Literal["16:9", "9:16", "1:1"] = "16:9"
    glossary: dict[str, str] = Field(
        default_factory=dict,
        description="术语表：保证同一概念在全片中的译名一致",
    )
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _apply_preset(self) -> StyleGuide:
        """把预设展开成具体色值；显式给出的颜色优先。

        未登记的预设**直接报错**，不静默回落到 default：静默回落的表现是
        "用户选了暖色调，成片却是蓝色"，而且没有任何地方提示过 ——
        与本项目在调色方案上采取的态度一致。
        """
        if self.preset not in STYLE_PRESETS:
            raise ValueError(
                f"未知的风格预设 {self.preset!r}；可用：{', '.join(sorted(STYLE_PRESETS))}"
            )
        palette = STYLE_PRESETS[self.preset]
        if not self.primary_color:
            self.primary_color = palette["primary_color"]
        if not self.background_color:
            self.background_color = palette["background_color"]
        return self

    @field_validator("background_style")
    @classmethod
    def _check_background_style(cls, v: str) -> str:
        """背景样式必须在册。

        延迟导入 `backgrounds`：那个模块需要引用本模块的 StyleGuide（仅类型），
        而本模块要在校验时用它的表 —— 模块层互相导入会变成循环导入。

        未登记的样式**直接报错**，理由与风格预设一致：静默回落的表现是
        "用户选了网格、成片却是纯色"，且没有任何地方提示过。
        """
        from .backgrounds import BACKGROUND_STYLE_IDS

        key = (v or "").strip().lower() or "auto"
        if key not in BACKGROUND_STYLE_IDS:
            raise ValueError(
                f"未知的背景样式 {v!r}；可用：{', '.join(BACKGROUND_STYLE_IDS)}"
            )
        return key

    @property
    def resolution(self) -> tuple[int, int]:
        """由宽高比推导分辨率，保证与渲染配置一致。"""
        return {
            "16:9": (1920, 1080),
            "9:16": (1080, 1920),
            "1:1": (1080, 1080),
        }[self.aspect_ratio]


class JobRequest(BaseModel):
    """一次生成请求（对应 RunPipelineRequest）。"""

    job_id: str
    raw_script: str = Field(min_length=1)
    style_guide: StyleGuide = Field(default_factory=StyleGuide)
    target_duration_sec: float = Field(default=90.0, gt=0, le=3600)
    max_attempts_per_shot: int = Field(default=3, ge=1, le=10)
    locale: str = "zh-CN"
    resume: bool = False
    checkpoint_thread_id: str = ""


class PipelineEventModel(BaseModel):
    """流水线事件（Python -> Go）。字段与 proto PipelineEvent 一一对应。"""

    job_id: str
    shot_id: str = ""
    node: str = ""
    status: str = ""
    message: str = ""
    attempt: int = 0
    shot_index: int = 0
    total_shots: int = 0
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    artifact: RenderArtifact | None = None
    feedback: CriticFeedback | None = None
    error: str = ""
    ts_unix_ms: int = 0
    payload_json: str = ""
