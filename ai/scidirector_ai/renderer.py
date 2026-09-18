"""渲染引擎调度：把「标签 -> 引擎」的确定性路由落到具体渲染实现上。

四种渲染路径，各有明确的适用场景与代价：

| 引擎 | 实现 | 依赖 | 典型耗时 | 适用标签 |
| --- | --- | --- | --- | --- |
| `manim` | **ManimSandbox**（沙盒子进程） | Python + Manim + LaTeX | 10~60s | MATH |
| `d3` / `echarts` | headless 浏览器逐帧截图 + ffmpeg 编码 | Playwright + Chromium | 5~30s | DATA |
| `code_anim` | 同 HTML 路径（高亮 + 打字动画） | Playwright + Chromium | 5~20s | CODE |
| `stock` | ffmpeg lavfi 动态渐变 | 仅 ffmpeg | < 2s | AMBIENCE |

分层约定：
    **本模块**负责「选哪个引擎、统一成败语义」；
    **sandbox/manim.py** 负责「怎么安全地跑起来」；
    **tools/media.py** 负责「怎么调 ffmpeg」。

**渲染不可用时的行为**：不是静默降级成占位视频，而是抛出带明确原因的
:class:`RendererError`。理由：产出"看起来成功但其实空白"的视频，
比直接失败并转人工危险得多 —— 后者会被发现，前者会流到成片里。
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .config import Settings, browser_ready, get_settings
from .logging import get_logger
from .sandbox.manim import ManimRenderRequest, ManimSandbox, ManimSandboxError
from .sandbox.policy import PolicyReport, PolicyViolation
from .sandbox.runner import SandboxRunner
from .media import MediaInfo, MediaToolError, encode_frames, probe, render_ambient

logger = get_logger(__name__)


class RendererError(RuntimeError):
    """渲染失败。

    ``retryable`` 区分两类失败，直接决定流水线是"再烧一轮"还是"立刻停下来找人"：

    * ``True``  —— 代码问题（语法错误、超时、内存超限）。把 stderr 回灌给
      编码智能体重写，有可能改对。
    * ``False`` —— 环境问题（没装 Manim / 浏览器 / 字体）。重试多少次都一样。

    这个区分直接决定流水线是"再烧一轮"还是"立刻停下来找人"，
    判错方向的代价极不对称。
    """

    def __init__(self, message: str, *, retryable: bool = True, detail: str = "") -> None:
        super().__init__(message)
        self.retryable = retryable
        self.detail = detail


@dataclass
class RenderRequest:
    """一次渲染请求。"""

    shot_id: str
    code: str
    output_dir: Path
    duration_sec: float
    width: int
    height: int
    fps: int
    #: 草稿模式：低分辨率快速验证，通过后再高清重渲。显著降低无效渲染的成本。
    draft: bool = False
    #: 氛围镜头要显示的文字（一般为空，标题镜头才用）。
    overlay_text: str = ""
    primary_color: str = "#4F8CFF"
    background_color: str = "#0B1020"
    #: 额外注入给子进程的环境变量（仅受信任的调用方，例如测试）。
    env_extra: dict[str, str] = field(default_factory=dict)

    #: ---- 局部重渲染 ----
    #: 只渲染 [range_start_sec, range_end_sec) 这一段，用于「只改了一处细节」。
    #:
    #: 只在**时间轴可控**的引擎上有意义：AMBIENCE（lavfi）与 HTML（逐帧 seek）
    #: 可以按秒精确截取；MANIM 是按动画序号驱动渲染的，
    #: 「第 3~5 秒」无法可靠映射到动画区间，因此会忽略该区间并整镜重渲。
    #:
    #: 注意局部渲染的语义是「产出正好覆盖该区间的一段视频」，
    #: 由 Go 侧负责把它拼回原片，Python 侧不关心拼接。
    range_start_sec: float = 0.0
    range_end_sec: float = 0.0

    @property
    def wants_range(self) -> bool:
        """是否请求了局部渲染。"""
        return self.range_end_sec > self.range_start_sec >= 0

    @property
    def effective_duration_sec(self) -> float:
        """本次实际需要渲染的时长。"""
        if self.wants_range:
            return self.range_end_sec - self.range_start_sec
        return self.duration_sec

    @property
    def effective_start_sec(self) -> float:
        """本次渲染在镜头时间轴上的起点。"""
        return self.range_start_sec if self.wants_range else 0.0


@dataclass
class RenderResult:
    """渲染产物。"""

    video_path: str
    engine: str
    media: MediaInfo
    render_cost_sec: float
    logs: str = ""
    peak_memory_mb: float | None = None
    #: 渲染过程中产生的中间目录，便于失败时人工排查。
    work_dir: str = ""
    #: 本次是否真的只渲染了请求的区间（引擎不支持时为 False）。
    #: 必须如实回填：Go 侧据此决定是拼接回原片还是整镜替换。
    partial_range_honored: bool = False

    # 便捷只读属性：让调用方不必每次都写 result.media.xxx
    @property
    def duration_sec(self) -> float:
        return self.media.duration_sec

    @property
    def width(self) -> int:
        return self.media.width

    @property
    def height(self) -> int:
        return self.media.height

    @property
    def fps(self) -> float:
        return self.media.fps


class Renderer(Protocol):
    """渲染器协议。每个引擎实现一个。"""

    engine: str

    def available(self) -> tuple[bool, str]:
        """返回 (是否可用, 不可用原因)。"""
        ...

    def render(self, request: RenderRequest, runner: SandboxRunner) -> RenderResult:
        ...


# ---------------------------------------------------------------------------
# Manim（数学镜头）
# ---------------------------------------------------------------------------


class ManimRenderer:
    """Manim 渲染器 —— 委托给 :class:`ManimSandbox` 执行。

    刻意**不**在这里重复实现进程管理、超时、内存限制与产物校验：
    那些是安全关键逻辑，只能有一份实现（在 sandbox/manim.py）。
    本类只负责把统一的 :class:`RenderRequest` 翻译成沙盒的请求，
    并把沙盒的失败语义翻译成统一的 :class:`RendererError`。
    """

    engine = "manim"

    def __init__(self, settings: Settings, sandbox: ManimSandbox | None = None) -> None:
        self.settings = settings
        self._sandbox = sandbox

    @property
    def sandbox(self) -> ManimSandbox:
        """惰性构造沙盒（健康检查时不必付这份开销）。"""
        if self._sandbox is None:
            self._sandbox = ManimSandbox(self.settings)
        return self._sandbox

    def available(self) -> tuple[bool, str]:
        return self.sandbox.check_environment()

    def render(self, request: RenderRequest, runner: SandboxRunner) -> RenderResult:
        # 让沙盒用调用方指定的 runner（便于测试注入与统一并发控制）。
        sandbox = self._sandbox or ManimSandbox(self.settings, runner)
        self._sandbox = sandbox

        try:
            outcome = sandbox.render(
                ManimRenderRequest(
                    code=request.code,
                    output_dir=request.output_dir,
                    expected_duration_sec=request.duration_sec,
                    draft=request.draft,
                    env_extra=request.env_extra,
                )
            )
        except ManimSandboxError as exc:
            # 保留沙盒给出的 retryable 判定与诊断细节 —— 它是唯一知道
            # "到底是超时、内存超限，还是缺依赖"的一层。
            raise RendererError(
                str(exc),
                retryable=exc.retryable,
                detail=exc.detail or (exc.policy.summary() if exc.policy else ""),
            ) from exc

        # Manim **不支持**局部重渲染，必须如实上报 partial_range_honored=False。
        #
        # 原因是语义层面的，不是实现层面的：Manim 按**动画序号**驱动渲染，
        # 而「第 3~5 秒」到动画区间的映射取决于每个动画各自的耗时，
        # 无法在不完整渲染的前提下可靠求出。
        # 硬做的话只能"先整镜渲染再截取"，那样没有任何成本收益，
        # 反而制造出「以为省了、其实没省」的错觉。
        # 因此这里整镜渲染，并由 Go 侧据此走整体替换。
        if request.wants_range:
            logger.debug(
                "Manim 不支持局部重渲染，改为整镜渲染",
                extra={"shot_id": request.shot_id},
            )

        return RenderResult(
            video_path=outcome.video_path,
            engine=self.engine,
            media=outcome.media,
            render_cost_sec=outcome.render_cost_sec,
            logs=outcome.stdout_tail,
            peak_memory_mb=outcome.peak_memory_mb,
            work_dir=str(request.output_dir),
            partial_range_honored=False,
        )


# ---------------------------------------------------------------------------
# HTML / headless 浏览器（数据与代码镜头）
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 浏览器就绪探测
# ---------------------------------------------------------------------------

# 探测的**唯一实现**在 config.browser_ready()：健康检查（toolchain_report）
# 与渲染前的自检必须取同一份结论，否则又会出现「健康说可用、渲染说不可用」
# 这种两边各说各话的情况。这里只做转发，不再复制一份逻辑。


class HtmlRenderer:
    """基于 headless 浏览器的渲染器（D3 / ECharts / 代码动画共用）。

    **逐帧截图**而不是录屏：录屏会丢帧、抖动，且不同机器结果不一致；
    逐帧截图配合页面暴露的 ``window.__seek(t)`` 是确定性的 ——
    同一份代码在任何机器上产出同一段视频。
    """

    def __init__(self, settings: Settings, engine: str) -> None:
        self.settings = settings
        self.engine = engine

    def available(self) -> tuple[bool, str]:
        # 用 config 的统一探测：健康检查与渲染自检必须看同一份结论。
        ok, reason = browser_ready()
        if not ok:
            return False, reason
        if not shutil.which("ffmpeg"):
            return False, "缺少 ffmpeg，无法把帧序列编码为视频"
        return True, ""

    def render(self, request: RenderRequest, runner: SandboxRunner) -> RenderResult:
        available, reason = self.available()
        if not available:
            # 环境问题：重试没有意义，必须转人工。
            raise RendererError(reason, retryable=False)

        work = Path(request.output_dir).resolve()
        frames_dir = work / "frames"
        if frames_dir.exists():
            shutil.rmtree(frames_dir, ignore_errors=True)
        frames_dir.mkdir(parents=True, exist_ok=True)

        html = work / "index.html"
        html.write_text(_wrap_html(request), encoding="utf-8")

        fps = request.fps
        # 局部重渲染：只截取请求的区间，帧号从该区间的起点开始计。
        # 时间轴可控（我们逐帧驱动 __seek(t)），所以「按秒截取」在这里是精确的。
        honored = request.wants_range
        start_sec = request.effective_start_sec
        render_duration = request.effective_duration_sec
        total_frames = max(int(round(render_duration * fps)), 1)
        started = time.monotonic()

        try:
            self._capture(html, frames_dir, total_frames, fps, request, start_sec=start_sec)
        except RendererError:
            raise
        except Exception as exc:  # noqa: BLE001 - Playwright 异常类型繁多
            raise RendererError(
                f"浏览器渲染失败：{exc}", retryable=True, detail=str(exc)[:1200]
            ) from exc

        mp4 = work / f"{request.shot_id}_{self.engine}.mp4"
        try:
            encode_frames(frames_dir, mp4, runner, fps=fps, duration_sec=render_duration)
            info = probe(mp4, runner)
        except MediaToolError as exc:
            # 统一归一化为 RendererError：让流水线只需处理一种失败形态，
            # 否则 MediaToolError 会穿透到图外层，把"单镜头失败"
            # 升级成"整个任务失败"（本项目已踩过一次）。
            raise RendererError(f"帧序列编码失败：{exc}", retryable=True) from exc

        cost = time.monotonic() - started
        return RenderResult(
            video_path=str(mp4),
            engine=self.engine,
            media=info,
            render_cost_sec=cost,
            logs=f"逐帧截图 {total_frames} 帧 @ {fps}fps"
            + (f"（局部 {start_sec:.2f}s~{start_sec + render_duration:.2f}s）" if honored else ""),
            work_dir=str(work),
            partial_range_honored=honored,
        )

    def _capture(
        self,
        html: Path,
        frames_dir: Path,
        total_frames: int,
        fps: int,
        request: RenderRequest,
        start_sec: float = 0.0,
    ) -> None:
        """用 Playwright 逐帧截图。"""
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                # 容器里需要这些参数：没有 /dev/shm 与沙盒权限时 Chromium 会直接崩。
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            try:
                page = browser.new_page(
                    viewport={"width": request.width, "height": request.height},
                    device_scale_factor=1,
                )
                page.goto(html.resolve().as_uri(), wait_until="load")
                # 等页面自报就绪（脚本可能异步加载字体等）。
                page.wait_for_function("() => window.__ready !== false", timeout=30_000)

                if page.evaluate("() => typeof window.__seek !== 'function'"):
                    raise RendererError(
                        "页面未定义 window.__seek(t)，无法逐帧渲染",
                        retryable=True,
                        detail=(
                            "HTML 渲染路径要求页面暴露 window.__seek = (t) => {...}，"
                            "t 为 0..duration_sec 的秒数。请参考提示词中的模板。"
                        ),
                    )

                for index in range(total_frames):
                    # 局部重渲染时，帧号从区间起点开始计：
                    # 第 0 帧对应 start_sec，而不是整镜的第 0 秒。
                    # 这里漏加偏移是最典型的错误 —— 画面能出来、时长也对，
                    # 但内容整体前移了 start_sec，且只有把片段拼回原片才看得出来。
                    page.evaluate("(t) => window.__seek(t)", start_sec + index / fps)
                    # 等待 rAF 完成一帧，避免截到动画中间态。
                    page.evaluate("() => new Promise(r => requestAnimationFrame(() => r()))")
                    page.screenshot(path=str(frames_dir / f"frame_{index:05d}.png"))
            finally:
                browser.close()


# ---------------------------------------------------------------------------
# 氛围镜头（ffmpeg lavfi）
# ---------------------------------------------------------------------------


class AmbientRenderer:
    """氛围镜头渲染器：ffmpeg 动态渐变（可选文字）。

    这是整条流水线里**最可靠**的路径：只依赖 ffmpeg。
    没有它则该引擎不可用；但只要有它，任何环境下都能出片 ——
    因此它也是 AMBIENCE 标签的兜底引擎，以及整条链路的"最小可验证单元"。
    """

    engine = "stock"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def available(self) -> tuple[bool, str]:
        if not shutil.which("ffmpeg"):
            return False, "缺少 ffmpeg，氛围镜头无法渲染"
        return True, ""

    def render(self, request: RenderRequest, runner: SandboxRunner) -> RenderResult:
        available, reason = self.available()
        if not available:
            raise RendererError(reason, retryable=False)

        work = Path(request.output_dir).resolve()
        work.mkdir(parents=True, exist_ok=True)
        mp4 = work / f"{request.shot_id}_ambient.mp4"

        # 局部重渲染：lavfi 源的相位由整镜时长决定，因此在滤镜链末尾 trim
        # （见 media._build_filtergraph 的说明），画面对齐整镜渲染，只编码窗口段。
        honored = request.wants_range
        started = time.monotonic()
        try:
            render_ambient(
                mp4,
                runner,
                duration_sec=request.duration_sec,
                width=request.width,
                height=request.height,
                fps=request.fps,
                colors=(
                    _hex_to_ffmpeg(request.background_color),
                    _hex_to_ffmpeg(request.primary_color),
                    _hex_to_ffmpeg(_shift(request.primary_color)),
                ),
                text=request.overlay_text,
                window_start_sec=request.range_start_sec if honored else 0.0,
                window_end_sec=request.range_end_sec if honored else 0.0,
            )
            info = probe(mp4, runner)
        except MediaToolError as exc:
            # 见 HtmlRenderer 中的同类说明：所有渲染失败都必须归一化为
            # RendererError，否则会穿透成任务级失败。
            raise RendererError(f"氛围镜头渲染失败：{exc}", retryable=True) from exc
        cost = time.monotonic() - started

        return RenderResult(
            video_path=str(mp4),
            engine=self.engine,
            media=info,
            render_cost_sec=cost,
            logs="ffmpeg lavfi 动态渐变"
            + (
                f"（局部 {request.range_start_sec:.2f}s~{request.range_end_sec:.2f}s）"
                if honored
                else ""
            ),
            work_dir=str(work),
            partial_range_honored=honored,
        )


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------

#: 引擎 -> 渲染器实现。
#: 新增引擎必须同时在此登记，否则 ``build_renderer`` 会明确报错而不是静默降级。
_RENDERERS: dict[str, type] = {
    "manim": ManimRenderer,
    "d3": HtmlRenderer,
    "echarts": HtmlRenderer,
    "code_anim": HtmlRenderer,
    "stock": AmbientRenderer,
}

#: 需要 LLM 生成代码的引擎（`stock` 不需要 —— 由 ffmpeg 程序化生成）。
LLM_ENGINES: frozenset[str] = frozenset({"manim", "d3", "echarts", "code_anim"})

#: 走 HTML 逐帧截图路径的引擎。
HTML_ENGINES: frozenset[str] = frozenset({"d3", "echarts", "code_anim"})


def build_renderer(engine: str, settings: Settings | None = None) -> Renderer:
    """按引擎名构造渲染器。未知引擎给出明确错误，而不是静默降级。"""
    factory = _RENDERERS.get(engine)
    if factory is None:
        raise RendererError(
            f"未知渲染引擎 {engine!r}；已登记：{sorted(_RENDERERS)}", retryable=False
        )
    cfg = settings or get_settings()
    if factory is HtmlRenderer:
        return HtmlRenderer(cfg, engine)
    return factory(cfg)  # type: ignore[call-arg]


def renderer_availability(settings: Settings | None = None) -> dict[str, bool]:
    """一次性探测所有引擎的可用性，用于 /readyz 与启动日志。

    提前暴露"哪些标签当前渲染不了"，比让任务跑到那一步才失败要好得多。
    """
    cfg = settings or get_settings()
    report: dict[str, bool] = {}
    for engine in _RENDERERS:
        try:
            ok, _ = build_renderer(engine, cfg).available()
        except RendererError:  # pragma: no cover - 理论上不可达
            ok = False
        report[engine] = ok
    return report


# ---------------------------------------------------------------------------
# HTML 渲染契约的静态检查
# ---------------------------------------------------------------------------

#: HTML 渲染路径的强制契约。
_HTML_REQUIRED_MARKERS = ("window.__seek",)

#: 明确会破坏沙盒（无网络）的写法。
_HTML_FORBIDDEN_MARKERS = (
    "cdn.jsdelivr.net",
    "cdnjs.cloudflare.com",
    "unpkg.com",
    "d3js.org",
    "echarts.apache.org",
    "//ajax.googleapis.com",
)


def check_html_contract(code: str) -> PolicyReport:
    """HTML 渲染路径的静态检查。

    与 Python 的 AST 检查不同，这里检查的是**渲染契约**：

    * 必须定义 ``window.__seek`` —— 没有它，逐帧截图只会得到一张静止画面，
      而**渲染本身会"成功"**，问题直到 VLM 审查才发现（浪费一整轮渲染 + 调用）；
    * 不能引用 CDN —— 沙盒无网络，脚本静默加载失败同样会产出空白画面。

    两条的共同点是：**失败是静默的**。因此必须在渲染前用静态检查拦住。
    """
    report = PolicyReport()
    if not code or not code.strip():
        report.violations.append(
            PolicyViolation(
                reason="HTML 代码为空",
                severity="error",
                advice="请输出完整的 HTML 或 <script> 片段，并实现 window.__seek(t)。",
            )
        )
        return report

    for marker in _HTML_REQUIRED_MARKERS:
        if marker not in code:
            report.violations.append(
                PolicyViolation(
                    # reason 给人看（简短）；advice 回灌给模型（完整可执行）。
                    # 两者必须分开：契约类违规是"缺少某物"，套用"使用了被禁止的 X"
                    # 的默认措辞会生成病句，而那条文本正是模型改错的唯一线索。
                    reason=f"缺少渲染契约 {marker}(t)",
                    severity="error",
                    snippet=marker,
                    advice=(
                        f"页面缺少渲染契约：必须在脚本里定义 `{marker} = (t) => {{...}}`，"
                        "t 为 0 到 duration_sec 的秒数，用它把动画状态设置到第 t 秒。"
                        "同时需要 `window.__ready = true` 表示页面已就绪。"
                        "渲染器是逐帧截图，依赖这个函数推进动画。"
                    ),
                )
            )

    lowered = code.lower()
    for marker in _HTML_FORBIDDEN_MARKERS:
        if marker in lowered:
            report.violations.append(
                PolicyViolation(
                    reason=f"引用了外部 CDN（{marker}）",
                    severity="error",
                    snippet=marker,
                    advice=(
                        f"请移除对外部 CDN 的引用（{marker}）：执行环境**没有网络**，"
                        "脚本会静默加载失败并产出空白画面。"
                        "请改用纯 SVG / Canvas 手写绘制实现同样的图形。"
                    ),
                )
            )

    if "<canvas" in lowered and "getcontext" not in lowered:
        report.violations.append(
            PolicyViolation(
                reason="声明了 canvas 但没有获取 2D 上下文",
                severity="warning",
                snippet="canvas",
                advice=(
                    "声明了 <canvas> 却没有调用 getContext('2d')，画面会保持空白。"
                    "请补上 `const ctx = canvas.getContext('2d')`，"
                    "或在 window.__seek 中完成绘制。"
                ),
            )
        )
    return report


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _hex_to_ffmpeg(color: str) -> str:
    """``#RRGGBB`` / ``#RGB`` -> ffmpeg 的 ``0xRRGGBB``。"""
    value = (color or "").strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        return "0x4F8CFF"  # 非法值回退到默认主色，而不是让渲染失败
    return "0x" + value.upper()


def _shift(color: str) -> str:
    """把主色向暖色偏移，作为渐变的第三个色停。

    纯程序化操作即可得到视觉上舒服的第三色，无需调色板配置。
    """
    value = (color or "").strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        return "#FF6B6B"
    try:
        r = min(255, int(value[0:2], 16) + 80)
        g = max(0, int(value[2:4], 16) - 20)
        b = max(0, int(value[4:6], 16) - 40)
    except ValueError:
        return "#FF6B6B"
    return f"#{r:02X}{g:02X}{b:02X}"


def _wrap_html(request: RenderRequest) -> str:
    """把编码智能体产出的片段包装成完整 HTML。

    约定：生成内容可以是完整 HTML 文档，也可以只是一段 ``<script>``/``<style>``
    片段；前者直接使用，后者注入到统一的外壳中。
    这样模型既能完全控制页面，又能在简单场景下少写模板代码。
    """
    code = request.code.strip()
    lowered = code.lower()
    if "<html" in lowered or "<!doctype" in lowered:
        return code

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
  html, body {{
    margin: 0; padding: 0; width: 100%; height: 100%;
    background: {request.background_color};
    color: #FFFFFF;
    font-family: "Noto Sans CJK SC", "Microsoft YaHei", system-ui, sans-serif;
    overflow: hidden;
  }}
  #stage {{ position: absolute; inset: 0; }}
</style>
</head>
<body>
<div id="stage"></div>
{code}
</body>
</html>
"""


#: 供测试与文档引用：引擎到标签的映射（与 Go 侧 domain.EngineForTag 一致）。
ENGINE_BY_TAG: dict[str, str] = {
    "MATH": "manim",
    "DATA": "d3",
    "CODE": "code_anim",
    "AMBIENCE": "stock",
}
