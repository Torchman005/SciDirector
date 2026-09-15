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
    AMBIENCE = "AMBIENCE"  # [氛围] 过渡、情绪铺陈 -> 素材 / 渐变占位

    @classmethod
    def from_prompt(cls, raw: str) -> SceneTag:
        """把模型输出的自由文本标签归一化。

        模型经常会输出 ``[数学]`` / ``math`` / ``MATH`` 等变体，
        统一在这里归一，避免调用点各写一遍兼容逻辑。
        """
        text = (raw or "").strip().strip("[]【】").upper()
        aliases = {
            "数学": cls.MATH, "公式": cls.MATH, "MATH": cls.MATH, "MATHSCENE": cls.MATH,
            "数据": cls.DATA, "图表": cls.DATA, "DATA": cls.DATA, "CHART": cls.DATA,
            "代码": cls.CODE, "编程": cls.CODE, "CODE": cls.CODE, "CODING": cls.CODE,
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


# 标签 -> 引擎的确定性路由表。
# 与 Go 侧 ``domain.EngineForTag`` 必须保持一致（见 Agent.md §5.2）。
TAG_TO_ENGINE: dict[SceneTag, RenderEngine] = {
    SceneTag.MATH: RenderEngine.MANIM,
    SceneTag.DATA: RenderEngine.D3,
    SceneTag.CODE: RenderEngine.CODE_ANIM,
    SceneTag.AMBIENCE: RenderEngine.STOCK,
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


class CriticFeedback(BaseModel):
    """审查意见（VLM 或人类）。"""

    passed: bool = False
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    issues: list[str] = Field(default_factory=list, description="发现的问题")
    suggestions: list[str] = Field(
        default_factory=list,
        description="【必须可执行】例如「字号 24 -> 48」；禁止「画面不好看」这类不可执行意见",
    )
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


class StyleGuide(BaseModel):
    """风格约束。跨镜头一致性靠它维持（避免每个镜头风格漂移）。"""

    theme: str = Field(default="dark", description="dark / light")
    primary_color: str = "#4F8CFF"
    background_color: str = "#0B1020"
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
