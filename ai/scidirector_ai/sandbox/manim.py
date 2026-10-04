"""Manim 渲染沙盒：在受控子进程中执行 LLM 生成的数学动画代码。

这是"执行不可信代码"这条风险链上的**第一等公民**。一次渲染的完整防护是四层：

    第 0 层  静态白名单    sandbox/policy.py（AST 扫描，**进程启动之前**就拦住）
    第 1 层  进程隔离      独立会话/进程组，超时杀掉**整棵进程树**
    第 2 层  资源限制      30s 墙钟超时 + 内存上限 + CPU 时间 + 进程数上限
    第 3 层  产物校验      必须存在、非空、能被 ffprobe 解析，否则判为渲染失败

## 为什么超时和内存限制都必须有

"防止死循环导致系统崩溃"这句话包含两类完全不同的崩溃：

* **死循环**吃满 CPU、永不返回 —— 靠 30s 墙钟超时 + SIGKILL 整棵进程树解决；
* **内存爆炸**（例如 `while True: list.append("x"*10000)`）在几秒内就能把
  机器打爆，此时超时根本来不及触发 —— 靠 RLIMIT_AS / Job Object + 监控线程解决。

只做其中一个都会漏掉另一半。

## 为什么必须杀整棵进程树

Manim 渲染时会派生 LaTeX / dvisvgm 子进程。只杀父进程会留下孤儿进程
继续吃 CPU —— 表现为"沙盒明明报超时了，机器风扇却还在狂转"，
这是最容易被忽略、也最难排查的一类资源泄漏。
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings, get_settings
from ..logging import get_logger
from ..sandbox.policy import PolicyReport, check_source
from ..sandbox.runner import ExecResult, ResourceLimits, SandboxRunner
from ..media import MediaInfo, MediaToolError, probe

logger = get_logger(__name__)

#: manim 质量预设 -> (宽, 高, 帧率)。
#:
#: **只用于草稿渲染**（`draft=True`）：正式渲染一律使用配置里的
#: `render_width/render_height/render_fps`，见 ``ManimSandbox.render``。
#: 保留这张表是为了让草稿模式也能走 ``-r/--fps`` 这条路 ——
#: 同一份代码只有一条设置分辨率的途径，才不会出现"正式渲染悄悄用了预设"这种事。
QUALITY_PRESETS: dict[str, tuple[int, int, int]] = {
    "l": (854, 480, 15),
    "m": (1280, 720, 30),
    "h": (1920, 1080, 60),
    "k": (3840, 2160, 60),
}


class ManimSandboxError(RuntimeError):
    """Manim 渲染失败。

    ``retryable`` 区分两类失败，直接决定流水线是"再烧一轮"还是"立刻停下来找人"：

    * ``True``  —— 代码问题（语法错误、LaTeX 报错、超时、内存超限）。
      把 stderr 回灌给编码智能体重写，有可能改对。
    * ``False`` —— 环境问题（没装 Manim / LaTeX）。重试多少次都一样。

    判错方向的代价很不对称：把环境问题判成可重试，会无限烧钱；
    把代码问题判成不可重试，会让本来能修好的镜头统统转人工。
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        detail: str = "",
        killed_reason: str = "",
        policy: PolicyReport | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.detail = detail
        self.killed_reason = killed_reason
        self.policy = policy

    def feedback(self) -> str:
        """给编码智能体的可执行反馈。

        静态违规与运行期报错要分开表述：前者要它**改写法**，
        后者要它**改逻辑**。混在一起会给模型含糊的信号。
        """
        parts = [str(self)]
        if self.policy is not None and self.policy.violations:
            parts.append("静态检查未通过：" + self.policy.summary())
        if self.detail:
            # 上限从 1500 放宽到 2600：detail 现在是「【LaTeX 错误】+【原始输出尾部】」
            # 两段，1800 + 1200 的预算需要它才装得下。
            # 顺序上 LaTeX 错误在前，所以即使再被截，**真因也一定留得住** ——
            # 原先那种"从头截 1500 字符"会把整段画框 traceback 塞满，
            # 真因（`! Undefined control sequence.`）一个字都进不来。
            parts.append("渲染输出：\n" + self.detail[:2600])
        return "\n\n".join(parts)


#: LaTeX 的错误行以 `!` 开头，其后数行是出错上下文（宏展开 / `l.<行号>`）。
_TEX_ERROR_LINE = re.compile(r"^!.*$", re.MULTILINE)

#: 每条 `!` 错误向后多带几行上下文。LaTeX 会把「出错位置」与一段宏展开
#: 紧跟在错误行之后，只给错误行本身往往看不出该改哪里。
_TEX_ERROR_CONTEXT_LINES = 6

#: 提取结果的总长度上限。日志可能有几千行，但真正可执行的信息只有开头那几行。
_TEX_ERROR_MAX_CHARS = 1800


def _extract_tex_errors(work: Path, output: str = "") -> str:
    """从渲染目录里的 TeX 日志中提取**真正的** LaTeX 错误。

    为什么必须单独做这件事：manim 把 LaTeX 的诊断写进 ``media/Tex/<hash>.log``，
    而它的 stdout/stderr 只有 Python 的 traceback。

    实测（一个未定义控制序列的用例）：捕获到的 **2001 字符输出里一条 `!` 错误行
    都没有**，而同一目录的日志里赫然写着 ``! Undefined control sequence.``。

    后果有两层，都很贵：

    * **用户**看到的是一段指向 `tex_file_writing.py` 的 traceback，
      真因（哪一行 LaTeX 错了）完全不可见 —— 连贴出来求助都贴不到重点；
    * **编码智能体**拿到的反馈里同样没有真因，它只能瞎改，
      于是白烧一轮渲染 + 两次模型调用，而这正是"失败率偏高"与
      "打回重做效果不好"的共同来源。

    返回空串表示没找到（例如失败与 LaTeX 无关）。
    """
    blocks: list[str] = []

    # 1) 先看日志文件（真因几乎总在这里）。
    try:
        logs = sorted(work.rglob("*.log"))
    except OSError:  # pragma: no cover - 目录被删/无权限时不该让报错本身失败
        logs = []
    for log in logs[:4]:
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for block in _tex_error_blocks(text):
            blocks.append(f"{block}\n（来自 {log.name}）")

    # 2) 再看捕获到的输出：有些路径下 manim 会把 LaTeX 输出一并打出来。
    for block in _tex_error_blocks(output):
        blocks.append(block)

    if not blocks:
        return ""

    # 去重（stdout 与日志常常重复同一条），并限制总长度。
    seen: set[str] = set()
    unique: list[str] = []
    for b in blocks:
        key = b.splitlines()[0] if b else b
        if key in seen:
            continue
        seen.add(key)
        unique.append(b)

    joined = "\n\n".join(unique)[:_TEX_ERROR_MAX_CHARS]
    return "【LaTeX 错误】\n" + joined


def _tex_error_blocks(text: str) -> list[str]:
    """把 ``!`` 错误行连同其后若干行上下文切成块。"""
    if "!" not in text:
        return []
    lines = text.splitlines()
    blocks: list[str] = []
    for i, line in enumerate(lines):
        if not line.startswith("!"):
            continue
        ctx = lines[i : i + 1 + _TEX_ERROR_CONTEXT_LINES]
        blocks.append("\n".join(part.rstrip() for part in ctx if part.strip()))
    return blocks


def _first_tex_error_line(tex_errors: str) -> str:
    """从提取结果里取第一行错误，用于拼进**面向人**的那句 message。"""
    for line in tex_errors.splitlines():
        if line.startswith("!"):
            return line.strip()[:140]
    return ""


@dataclass
class ManimRenderRequest:
    """一次 Manim 渲染请求。"""

    code: str
    output_dir: Path
    #: 期望时长（秒）。仅用于日志与后续审查，Manim 自身由代码里的动画决定。
    expected_duration_sec: float = 0.0
    #: 目标分辨率与帧率。**必须显式传给 manim**，否则只能拿到 `-q` 预设值。
    width: int = 1920
    height: int = 1080
    fps: int = 30
    #: 渲染质量：``l``/``m``/``h``/``k``。留空则用配置值。
    quality: str = ""
    #: 草稿模式：用最低质量快速验证，通过后再高清重渲（显著降低无效渲染成本）。
    draft: bool = False
    #: 保留上一轮媒体目录。默认清除，避免旧产物被误判成本次成功。
    keep_previous_media: bool = False
    #: 额外注入给子进程的环境变量。
    #: **仅限受信任的调用方**（例如测试注入假的 manim 模块）；
    #: 绝不能把 LLM 生成的任何内容传进来。
    env_extra: dict[str, str] = field(default_factory=dict)


@dataclass
class ManimRenderResult:
    """渲染产物。"""

    video_path: str
    scene_class: str
    media: MediaInfo
    render_cost_sec: float
    stdout_tail: str = ""
    peak_memory_mb: float | None = None
    timeout_sec: float = 0.0
    #: 是否触发了草稿渲染（供上层决定是否需要高清重渲）。
    draft: bool = False


#: 匹配 ``class XxxScene(Scene)`` / ``class Xxx(manim.Scene)``。
_SCENE_CLASS = re.compile(r"^\s*class\s+(\w+)\s*\([^)]*Scene[^)]*\)\s*:", re.MULTILINE)

#: 提示词约定的固定类名。找不到类定义时回退到它，
#: 让"类名不对"这类问题退化为可预期的报错，而不是让沙盒直接崩。
DEFAULT_SCENE_CLASS = "SciShotScene"

#: Manim 的 ``-q`` 质量等级 -> 对应的输出子目录名。
#: 我们不拼产物路径（版本间结构会变），只用它做日志；产物靠递归搜索定位。
QUALITY_LEVELS = {"l": "480p15", "m": "720p30", "h": "1080p60", "k": "2160p60"}

#: 判断"环境缺依赖"的特征串。命中则判为**不可重试**。
_ENV_ERROR_MARKERS = (
    "no module named 'manim'",
    'no module named "manim"',
    "no module named manim",
    "modulenotfounderror: no module named",
    "is not recognized as an internal or external command",
    "找不到可执行文件",
)

#: 判断"内存耗尽"的特征串。
#:
#: 为什么两个内存限制机制需要在这里**归一化**：
#: * 监控线程路径 -> 进程被 SIGKILL，``killed_reason="memory"``；
#: * 内核限制路径（RLIMIT_AS / Job Object）-> 分配失败，子进程抛 MemoryError
#:   后自行退出，``killed_reason`` 本会是空串。
#:
#: 同一个"内存超限"在两种机制下表现不同，上层就得写两套判断。
#: 这里把它统一成同一种可观测结果，调用方只需要看 ``killed_reason``。
_MEMORY_ERROR_MARKERS = (
    "memoryerror",
    "unable to allocate",
    "cannot allocate memory",
    "out of memory",
    "std::bad_alloc",
    "tex capacity exceeded",  # LaTeX 自己的内存上限，同样属于资源不足
)


class ManimSandbox:
    """基于 subprocess 的 Manim 渲染沙盒。"""

    def __init__(
        self,
        settings: Settings | None = None,
        runner: SandboxRunner | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.runner = runner or SandboxRunner()

    # ------------------------------------------------------------------
    # 环境探测（与渲染分离：健康检查可单独调用，渲染路径不必重复付探测开销）
    # ------------------------------------------------------------------

    def check_environment(self) -> tuple[bool, str]:
        """返回 (是否可用, 不可用原因)。"""
        try:
            import manim  # noqa: F401
        except ImportError:
            return False, "未安装 manim（pip install manim），数学镜头无法渲染"
        if not shutil.which("ffmpeg"):
            return False, "缺少 ffmpeg，Manim 无法合成视频"
        if not any(shutil.which(b) for b in ("latex", "xelatex", "pdflatex")):
            # 没有 LaTeX 时 Manim 仍能渲染 Text，但 MathTex/Tex 必失败。
            # 只告警不否决：纯文字镜头依然可以出片。
            logger.warning("未检测到 LaTeX，含公式的 Manim 场景会渲染失败")
        return True, ""

    @property
    def timeout_sec(self) -> float:
        """渲染墙钟超时。默认 30s（见 config.manim_timeout_sec 的说明）。"""
        return float(self.settings.manim_timeout_sec)

    @property
    def max_memory_mb(self) -> int:
        return int(self.settings.manim_max_memory_mb)

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------

    def render(self, request: ManimRenderRequest) -> ManimRenderResult:
        """在沙盒中渲染一段 Manim 代码。

        流程：静态检查 -> 写脚本 -> 子进程渲染 -> 校验产物 -> 探测元数据。
        """
        # --- 第 0 层：静态白名单（进程启动之前）-------------------------
        report = check_source(request.code)
        if not report.ok:
            # 这一步是毫秒级的，而一次真实渲染是数十秒。
            # 在进程启动前拦住 `import os`，省下的是整轮渲染时间。
            raise ManimSandboxError(
                "Manim 代码未通过静态安全检查，拒绝执行",
                retryable=True,
                detail=report.summary(),
                policy=report,
            )

        work = Path(request.output_dir).resolve()
        work.mkdir(parents=True, exist_ok=True)

        script = work / "scene.py"
        script.write_text(request.code, encoding="utf-8")

        scene_class = extract_scene_class(request.code)
        quality = request.quality or ("l" if request.draft else self.settings.sandbox_manim_quality)
        media_dir = work / "media"

        # 清掉上一轮的产物：否则递归搜索会命中旧文件，把失败误判成成功。
        if media_dir.exists() and not request.keep_previous_media:
            shutil.rmtree(media_dir, ignore_errors=True)

        # 分辨率与帧率**显式传给 manim**，不再用 `-q` 预设。
        #
        # 为什么：`-q` 只有几档固定预设，`-ql` 就是 854x480。这里过去只传 `-q`，
        # 于是成片永远是预设分辨率，配置里的 1920x1080 形同虚设 ——
        # 表现为"画面糊"，而且从日志里完全看不出是哪一层丢的。
        # 另外 `-r/--fps` 与 `-q` 都会设置分辨率，同时给出时谁生效取决于 manim
        # 内部的参数回调顺序（跨版本会变），不能依赖，所以这里**只传 `-r/--fps`**；
        # 草稿模式也用预设表换算成显式数值，两条路径完全一致。
        if request.draft:
            width, height, fps = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["l"])
        else:
            width, height, fps = request.width, request.height, request.fps

        argv = [
            self.settings.sandbox_python_bin, "-m", "manim",
            "-r", f"{width},{height}",
            "--fps", str(fps),
            "--format=mp4",
            "--media_dir", str(media_dir),
            # 关闭缓存与交互式预览：沙盒里没有显示设备，缓存还会干扰"是否真的重渲"的判断。
            "--disable_caching",
            str(script),
            scene_class,
        ]

        limits = ResourceLimits(
            timeout_sec=self.timeout_sec,
            max_memory_mb=self.max_memory_mb,
            # CPU 时间给超时的 2 倍：墙钟可能被 IO 等待拉长，
            # CPU 上限专门用来挡"不吃内存但死循环"的活锁。
            max_cpu_sec=int(self.timeout_sec * 2),
            max_processes=256,
        )

        logger.info(
            "Manim 沙盒开始渲染",
            extra={
                "scene_class": scene_class,
                "quality": quality,
                "resolution": f"{width}x{height}@{fps}",
                "draft": request.draft,
                "timeout_sec": limits.timeout_sec,
                "max_memory_mb": limits.max_memory_mb,
                "code_bytes": len(request.code),
            },
        )

        started = time.monotonic()
        result = self.runner.run(
            argv,
            cwd=work,
            limits=limits,
            env_extra=request.env_extra or None,
        )
        cost = time.monotonic() - started

        self._raise_if_failed(result, work=work)

        produced = newest_mp4(media_dir)
        if produced is None:
            # Manim 报成功但找不到产物：多半是场景类名写错（它默认渲染同名类）。
            raise ManimSandboxError(
                f"Manim 报告成功但未找到输出文件（场景类 {scene_class} 是否真的存在？）",
                retryable=True,
                detail=result.tail(1500),
            )

        # --- 第 3 层：产物校验 -------------------------------------------
        # 不校验会怎样：一个 0 字节或坏掉的 mp4 会被当成"渲染成功"进入审查，
        # 白烧一次 VLM 调用，最后在合成阶段才炸 —— 那时定位成本高得多。
        try:
            media = probe(produced, self.runner)
        except MediaToolError as exc:
            raise ManimSandboxError(
                f"Manim 产物无法解析：{exc}", retryable=True, detail=result.tail(1000)
            ) from exc

        if not media.valid:
            raise ManimSandboxError(
                "Manim 产物无效（分辨率为 0 或时长为 0）",
                retryable=True,
                detail=f"{media}\n{result.tail(800)}",
            )

        logger.info(
            "Manim 沙盒渲染完成",
            extra={
                "scene_class": scene_class,
                "video": str(produced),
                "duration_sec": round(media.duration_sec, 2),
                "resolution": f"{media.width}x{media.height}",
                "render_cost_sec": round(cost, 2),
                "peak_memory_mb": round(result.peak_memory_mb, 1) if result.peak_memory_mb else None,
            },
        )

        return ManimRenderResult(
            video_path=str(produced),
            scene_class=scene_class,
            media=media,
            render_cost_sec=cost,
            stdout_tail=result.tail(1500),
            peak_memory_mb=result.peak_memory_mb,
            timeout_sec=limits.timeout_sec,
            draft=request.draft,
        )

    # ------------------------------------------------------------------
    # 失败分类
    # ------------------------------------------------------------------

    def _raise_if_failed(self, result: ExecResult, *, work: Path) -> None:
        """把 ExecResult 翻译成带语义的异常。

        三种"失败"要给出完全不同的反馈：
        * 被杀（超时 / 内存超限）—— 上层需要知道是**资源**问题；
        * 环境缺依赖 —— 重试没有意义，必须转人工；
        * 代码报错 —— 把 stderr 回灌给编码智能体重写。
        """
        if result.killed_reason == "timeout":
            # 可能是墙钟超时，也可能是 CPU 时间预算先耗尽（RLIMIT_CPU = 超时×2，
            # 见下面的 limits 构造）。两者对上层是同一件事，但**该调哪个参数不一样**，
            # 所以提示里把两个开关都写出来：只提超时会让人反复调大超时而毫无效果。
            raise ManimSandboxError(
                f"Manim 渲染超时（>{self.timeout_sec:g}s）或 CPU 时间耗尽，已被强制终止"
                f"；若场景本身合法但偏慢，请调大 SCID_MANIM_TIMEOUT_SEC"
                f"（CPU 上限随之按 2 倍放大）或 SCID_SANDBOX_MAX_CPU_SEC",
                retryable=True,
                detail=result.tail(1200),
                killed_reason="timeout",
            )

        if result.killed_reason == "memory":
            peak = f"{result.peak_memory_mb:.0f}MB" if result.peak_memory_mb else "未知"
            raise ManimSandboxError(
                f"Manim 渲染内存超限（峰值 {peak}，上限 {self.max_memory_mb}MB）已被强制终止",
                retryable=True,
                detail=result.tail(1200),
                killed_reason="memory",
            )

        if result.ok:
            return

        stderr_lower = (result.stderr or "").lower()

        # 内存耗尽：内核限制路径不会"杀"进程，而是让分配失败后自行退出。
        # 归一化成与监控线程路径相同的 killed_reason，上层只需看一个字段。
        if any(marker in stderr_lower for marker in _MEMORY_ERROR_MARKERS):
            raise ManimSandboxError(
                f"Manim 渲染内存超限（上限 {self.max_memory_mb}MB，子进程报内存分配失败）",
                retryable=True,
                detail=result.tail(1500),
                killed_reason="memory",
            )

        missing_dep = any(marker in stderr_lower for marker in _ENV_ERROR_MARKERS)

        # 把**真正的** LaTeX 错误提到最前面。
        #
        # 顺序是刻意的：编码智能体拿到的 `feedback()` 会截断 detail，
        # 而真因如果排在几千字的 traceback 后面，等于没有。
        tex_errors = _extract_tex_errors(work, (result.stdout or "") + (result.stderr or ""))
        if tex_errors:
            detail = tex_errors + "\n\n【原始输出尾部】\n" + result.tail(1200)
            first = _first_tex_error_line(tex_errors)
            message = "Manim 渲染失败：" + result.summary()
            if first:
                # 也把真因写进 message：事件时间线里显示的是这一句，
                # 用户据此就能知道"是哪一行 LaTeX 错了"，而不必去翻沙盒输出。
                message += f"（{first}）"
        else:
            detail = result.tail(2000)
            message = "Manim 渲染失败：" + result.summary()

        raise ManimSandboxError(message, retryable=not missing_dep, detail=detail)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def extract_scene_class(code: str) -> str:
    """从 Manim 源码中提取 Scene 子类名。

    找不到时回退到约定类名（``SciShotScene``）：
    宁可让 Manim 报"找不到这个类"（错误信息明确、可回灌给编码智能体），
    也不要在这里抛异常把沙盒流程打断。
    """
    match = _SCENE_CLASS.search(code)
    if match:
        return match.group(1)
    logger.warning("Manim 代码中未识别到 Scene 子类，回退到约定类名", extra={"fallback": DEFAULT_SCENE_CLASS})
    return DEFAULT_SCENE_CLASS


def newest_mp4(root: Path) -> Path | None:
    """递归找出最新生成的 mp4。

    **不拼产物路径**：Manim 会写到
    ``<media_dir>/videos/<script_stem>/<quality_dir>/<ClassName>.mp4``，
    而 quality_dir 的名字随 ``-q`` 等级与版本变化。递归搜索对版本差异更健壮。
    """
    if not root.exists():
        return None
    candidates = [p for p in root.rglob("*.mp4") if p.is_file() and p.stat().st_size > 0]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)
