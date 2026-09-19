"""运行期配置装载。

设计原则与 Go 侧一致：
* 全部配置来自环境变量，且都有**可用默认值**（本地零配置可启动）；
* 校验在进程启动时一次性完成，配置错误快速失败；
* 敏感项（API Key）只在日志里以掩码形式出现。

为什么用 pydantic-settings 而不是裸 ``os.getenv``：
* 类型转换与校验由声明式 Field 完成，避免散落的 ``int(os.getenv(...))``；
* ``.env`` 文件天然支持，本地开发体验好；
* 字段有类型，IDE 与 mypy 能给出有效提示。
"""

from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 允许的日志级别，与 Go 侧保持一致。
LogLevel = Literal["debug", "info", "warning", "error"]


class Settings(BaseSettings):
    """进程级配置快照。字段名前缀统一为 ``SCID_``，与 docker-compose 对齐。"""

    model_config = SettingsConfigDict(
        env_prefix="SCID_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    env: Literal["dev", "staging", "prod", "test"] = "dev"
    log_level: LogLevel = "info"
    service_name: str = "scid-ai"

    # ------------------------------------------------------------------
    # 服务端口
    # ------------------------------------------------------------------
    ai_http_port: int = Field(default=8000, ge=1, le=65535)
    ai_grpc_port: int = Field(default=50051, ge=1, le=65535)
    ai_http_host: str = "0.0.0.0"
    ai_grpc_host: str = "0.0.0.0"
    # gRPC 线程池大小。渲染是阻塞型调用，线程数应显著大于 CPU 核数，
    # 但也要有上限，否则会把内存吃光（每个 Manim 进程都很重）。
    grpc_max_workers: int = Field(default=8, ge=1, le=64)

    # ------------------------------------------------------------------
    # 状态存储（与 Go 侧共用同一 Redis / Postgres）
    # ------------------------------------------------------------------
    redis_addr: str = "localhost:6379"
    redis_password: str = ""
    redis_db: int = 0

    postgres_dsn: str = "postgres://scidirector:scidirector@localhost:5432/scidirector?sslmode=disable"
    # LangGraph checkpoint 的 schema 名，便于与其他表隔离。
    checkpoint_schema: str = "langgraph"

    # ------------------------------------------------------------------
    # LLM / VLM
    # ------------------------------------------------------------------
    # ---------- 语音合成（TTS） ----------
    #
    # 默认**不合成**：缺省必须是一条能跑通的路径，没配 TTS 不该让流水线失败。
    # 只要配了 provider 且该服务商 available()，镜头就会多出一个配音文件，
    # 字幕随之切到按真实音频时长（有句级时间戳时按句）对齐。
    #
    # 各服务商的关键差别在**能不能拿到时间戳**：
    #   edge   —— 免费、无需密钥、给**句级**时间戳（本机唯一可真实验证的）
    #   doubao —— 需 appid + token（本机未验证）
    #   openai —— 需密钥，**不给时间戳**
    #   fish   —— 需密钥，另有带时间戳的 SSE 端点（负载结构未知，暂未实现）
    tts_provider: Literal["", "none", "edge", "doubao", "openai", "fish"] = ""
    #: 音色。留空则用各服务商的默认音色。
    tts_voice: str = ""
    #: 语速倍率，1.0 表示不变。
    tts_speed: float = Field(default=1.0, gt=0.0, le=3.0)
    tts_timeout_sec: int = Field(default=60, ge=5, le=600)
    # 重试：TTS 是网络调用，实测会遇到「连接被 reset」这类瞬时失败。
    # 只试一次会让大部分镜头悄悄失去配音（成片莫名没声音）。
    tts_max_attempts: int = Field(default=3, ge=1, le=10)
    tts_retry_backoff_sec: float = Field(default=1.0, ge=0.0, le=30.0)

    # 豆包（火山引擎）。cluster 与 voice_type 依账号开通情况而定。
    doubao_appid: str = ""
    doubao_access_token: str = ""
    doubao_cluster: str = "volcano_tts"
    doubao_endpoint: str = ""

    # Fish Audio。
    fish_api_key: str = ""
    fish_model: str = "s1"
    fish_reference_id: str = ""
    fish_endpoint: str = ""

    # OpenAI TTS（复用 openai_api_key / openai_base_url）。
    openai_tts_model: str = "gpt-4o-mini-tts"

    llm_provider: Literal["openai", "azure", "mock"] = "openai"
    llm_model: str = "gpt-4o"
    vlm_model: str = "gpt-4o"
    llm_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    llm_max_tokens: int = Field(default=4096, ge=256, le=128_000)
    llm_timeout_sec: int = Field(default=120, ge=5, le=1800)
    llm_max_retries: int = Field(default=3, ge=0, le=10)

    openai_api_key: str = ""
    openai_base_url: str = ""

    # ------------------------------------------------------------------
    # 沙盒与渲染
    # ------------------------------------------------------------------
    sandbox_timeout_sec: int = Field(default=180, ge=5, le=3600)
    sandbox_max_memory_mb: int = Field(default=2048, ge=128, le=32768)
    sandbox_max_cpu_sec: int = Field(default=240, ge=5, le=3600)
    sandbox_python_bin: str = "python"
    # Manim 渲染质量：l=480p 草稿（快，用于先验证再高清重渲）, m=720p, h=1080p
    sandbox_manim_quality: Literal["l", "m", "h", "k"] = "l"
    # 沙盒工作目录根；每个 job 在其下建独立子目录。
    sandbox_work_dir: str = "./.data/sandbox"

    # ------------------------------------------------------------------
    # Manim 渲染沙盒（数学镜头）
    # ------------------------------------------------------------------
    #: Manim 渲染的**墙钟超时**（秒）。到点即 SIGKILL 整棵进程树。
    #:
    #: 默认 30s：足以完成常规场景，同时能挡住死循环把机器拖死。
    #: 一个必须知道的取舍：**首次** LaTeX 编译可能就要 20~30s（生成字体格式），
    #: 所以冷启动环境下 30s 会偏紧。生产建议在镜像构建期预热 TeX 缓存
    #: （见 ai/Dockerfile），或用 SCID_MANIM_TIMEOUT_SEC 调大。
    #: 调太小会让合法但偏慢的场景被误杀，那比超时更糟 —— 因为它会静默地把
    #: 好镜头推给人工。
    manim_timeout_sec: int = Field(default=30, ge=5, le=1800)
    #: Manim 渲染沙盒的**内存上限**（MB）。超限即杀整棵进程树。
    #: LaTeX 是内存大户，且它由 Manim 派生 —— 因此限制必须覆盖整棵树
    #: （POSIX 靠 RLIMIT_AS 继承，Windows 靠 Job Object）。
    manim_max_memory_mb: int = Field(default=2048, ge=128, le=32768)

    render_fps: int = Field(default=30, ge=1, le=120)
    render_width: int = Field(default=1920, ge=128, le=7680)
    render_height: int = Field(default=1080, ge=128, le=4320)

    # ------------------------------------------------------------------
    # 审查阈值与熔断
    # ------------------------------------------------------------------
    critic_score_threshold: float = Field(default=0.75, gt=0.0, le=1.0)
    shot_max_attempts: int = Field(default=3, ge=1, le=10)
    # 抽帧数量：覆盖整体节奏；实现上还会额外补首末帧。
    critic_frame_samples: int = Field(default=4, ge=2, le=12)

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------
    prompts_dir: str = ""

    # ------------------------------------------------------------------
    # 派生属性与校验
    # ------------------------------------------------------------------
    @field_validator("sandbox_work_dir", "prompts_dir", mode="after")
    @classmethod
    def _expand_path(cls, v: str) -> str:
        """展开 ``~`` 与相对路径，避免不同工作目录下解析出不同结果。"""
        if not v:
            return v
        return str(Path(v).expanduser())

    @property
    def is_dev(self) -> bool:
        """开发态判定，用于放开调试能力（例如 mock LLM）。"""
        return self.env in ("dev", "test")

    @property
    def resolved_prompts_dir(self) -> Path:
        """提示词目录。默认取包内的 ``agents/prompts``。

        提示词与代码分离是刻意设计：便于版本化、灰度与 A/B，
        而不必为了改一句话去动 Python 逻辑。
        """
        if self.prompts_dir:
            return Path(self.prompts_dir)
        return Path(__file__).parent / "agents" / "prompts"

    def toolchain_report(self) -> dict[str, bool]:
        """探测外部工具链可用性，用于 /readyz 与启动日志。

        这些工具缺失不会让进程崩溃，但会让对应标签的镜头无法渲染，
        因此必须**显式暴露**而不是等到任务失败才发现。
        """
        report = {
            "python": shutil.which(self.sandbox_python_bin) is not None,
            "ffmpeg": shutil.which("ffmpeg") is not None,
            "ffprobe": shutil.which("ffprobe") is not None,
            "latex": any(shutil.which(b) for b in ("latex", "xelatex", "pdflatex")),
        }
        # manim 是纯 Python 包，导入探测就够。
        try:
            __import__("manim")
            report["manim"] = True
        except Exception:  # noqa: BLE001 - 环境相关，任何异常都视为不可用
            report["manim"] = False

        # playwright **不能**只看导入：浏览器二进制要另外装。
        # 只装第一步时报「可用」会让编排层把 d3/echarts/code_anim 镜头派出去，
        # 渲染时才失败 —— 白烧满 attempt 才熔断。详见 browser_ready() 的说明。
        report["playwright"], _ = browser_ready()
        return report

    def public_summary(self) -> dict[str, object]:
        """可安全写入日志的配置摘要（密钥一律掩码）。"""
        return {
            "env": self.env,
            "log_level": self.log_level,
            "http_port": self.ai_http_port,
            "grpc_port": self.ai_grpc_port,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "vlm_model": self.vlm_model,
            "tts_provider": self.tts_provider or "(未启用)",
            "tts_voice": self.tts_voice or "(默认)",
            "doubao_appid": self.doubao_appid or "(未配置)",
            "doubao_access_token": "***" if self.doubao_access_token else "(未配置)",
            "fish_api_key": "***" if self.fish_api_key else "(未配置)",
            "openai_api_key": "***" if self.openai_api_key else "(未配置)",
            "openai_base_url": self.openai_base_url or "(默认)",
            "sandbox_timeout_sec": self.sandbox_timeout_sec,
            "manim_timeout_sec": self.manim_timeout_sec,
            "manim_max_memory_mb": self.manim_max_memory_mb,
            "manim_quality": self.sandbox_manim_quality,
            "critic_score_threshold": self.critic_score_threshold,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程内单例配置。

    用 ``lru_cache`` 而非模块级常量：让测试可以在导入后通过
    ``get_settings.cache_clear()`` 重新装载配置。
    """
    return Settings()


# ---------------------------------------------------------------------------
# 渲染引擎可用性
# ---------------------------------------------------------------------------

#: Playwright 就绪探测的结果缓存。
#:
#: 健康检查会**按引擎各调一次**（d3 / echarts / code_anim），而每次探测都要启动
#: 一次 Playwright 的 driver 子进程（几十到几百毫秒），因此结果必须缓存 ——
#: 否则 `/healthz` 会被这些子进程拖慢，而它恰恰是最该轻快的接口。
#:
#: 代价：运行期**新装**浏览器后需要重启进程（或显式清缓存）才会被识别。
#: 这与字体探测的取舍一致，且远比「每次健康检查都起子进程」划算。
_browser_probe_cache: tuple[bool, str] | None = None


def _probe_browser_with(sync_playwright_factory) -> tuple[bool, str]:
    """给定 Playwright 工厂，判断 Chromium **二进制**是否真的可用。

    拆出可注入的工厂只是为了可测：真实调用方用 `browser_ready()`。
    """
    try:
        with sync_playwright_factory() as pw:
            exe = pw.chromium.executable_path
    except Exception as err:  # noqa: BLE001 - 环境相关，任何异常都视为不可用
        return False, f"playwright 无法初始化：{err}"
    if not exe or not os.path.exists(exe):
        return (
            False,
            "已装 playwright 但缺少 Chromium 二进制（请执行 playwright install chromium）",
        )
    return True, ""


def browser_ready() -> tuple[bool, str]:
    """Playwright 是否**真能**渲染（带缓存）。返回 (是否可用, 原因)。

    为什么不能只看 `import playwright`：Python 包与浏览器二进制是**两步**安装
    （`pip install playwright` + `playwright install chromium`）。只做完第一步时
    「导入探测」会报可用，于是 `engine_availability` 认为 d3/echarts/code_anim 都能渲染，
    编排层便放心地把这类镜头派出去 —— 到渲染时才失败，**白烧满 attempt 才熔断转人工**
    （实测 `#2 DATA attempt=3 → AWAITING_HUMAN`）。

    一个会说谎的就绪探测比没有探测更糟：它把「环境没准备好」伪装成
    「内容反复不达标」，而这两种情况的正确处置完全不同。
    """
    global _browser_probe_cache
    if _browser_probe_cache is None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            _browser_probe_cache = (
                False,
                "未安装 playwright（pip install playwright && playwright install chromium）",
            )
        else:
            _browser_probe_cache = _probe_browser_with(sync_playwright)
    return _browser_probe_cache


def reset_browser_probe_cache() -> None:
    """清除浏览器探测缓存：测试用；装完浏览器后也可主动重探而不重启。"""
    global _browser_probe_cache
    _browser_probe_cache = None


#: 渲染引擎 -> 它所需的工具链。改这里就等于改了「引擎可用性」的判定依据。
#:
#: 单独抽成常量（而不是散在判断里）的原因：这份映射同时被健康检查与
#: 运维文档引用，写成数据比写成 if 更容易核对与更新。
ENGINE_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    # 数学镜头：Manim 本身依赖 LaTeX 排版公式、依赖 ffmpeg 合成视频。
    "manim": ("manim", "latex", "ffmpeg"),
    # 数据 / 代码镜头：headless 浏览器逐帧截图，再用 ffmpeg 编码。
    "d3": ("playwright", "ffmpeg"),
    "echarts": ("playwright", "ffmpeg"),
    "code_anim": ("playwright", "ffmpeg"),
    # 氛围镜头：纯 ffmpeg lavfi 生成，依赖最少，因此也是最可靠的兜底路径。
    "stock": ("ffmpeg",),
}


def engine_availability(toolchain: dict[str, bool]) -> dict[str, bool]:
    """由工具链探测结果推导「每个渲染引擎是否可用」。

    纯函数、无副作用，因此可以被单测直接覆盖，也可以在日志与 /readyz 中复用。

    注意它回答的是**工具链就绪度**，而不是「渲染器是否已实现」：
    阶段一尚未实现任何渲染器（见 docs/ROADMAP.md），
    因此这里全为 True 也不代表现在就能出片。
    """
    return {
        engine: all(toolchain.get(tool, False) for tool in required)
        for engine, required in ENGINE_REQUIREMENTS.items()
    }
