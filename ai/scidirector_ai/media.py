"""ffmpeg / ffprobe 的底层调用。

设计约束（与 Go 侧 backend/internal/media 保持一致）：
    1. 所有外部进程调用都必须有超时，取消时杀整棵进程树；
    2. 命令一律用**列表**传参，不经过 shell（杜绝注入）；
    3. 失败时保留 stderr 尾部 —— 这是排查渲染问题最有价值的信息；
    4. 传给 ffmpeg 的路径一律**解析为绝对路径**（见下方说明）。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from .logging import get_logger
from .sandbox.runner import ResourceLimits, SandboxRunner

logger = get_logger(__name__)


class MediaToolError(RuntimeError):
    """ffmpeg / ffprobe 调用失败。"""


@dataclass
class MediaInfo:
    """ffprobe 的关键结论。"""

    path: str
    duration_sec: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    pix_fmt: str = ""
    has_audio: bool = False
    size_bytes: int = 0

    @property
    def valid(self) -> bool:
        """是否是一个可用的视频产物。

        判定刻意严格：分辨率为 0 或时长为 0 的"产物"在合成阶段会毁掉
        整条音画同步，宁可在此判为无效。
        """
        return self.width > 0 and self.duration_sec > 0


#: 常见中文字体候选路径。按顺序探测，第一个存在的被采用。
#: 之所以要探测而不是写死：Windows 开发机与 Linux 容器的字体路径完全不同。
_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
)


def find_font() -> str | None:
    """探测一个可用的中文字体文件。找不到返回 None（调用方应跳过文字绘制）。"""
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def _binary(name: str) -> str:
    """确认可执行文件存在，返回其路径。

    提前抛错而不是让 ffmpeg 调用失败：这样错误信息能明确指向
    "环境里没装 ffmpeg"，而不是一串难懂的 ffmpeg 报错。
    """
    path = shutil.which(name)
    if not path:
        raise MediaToolError(f"找不到可执行文件 {name}，请确认已安装并加入 PATH")
    return path


def probe(path: str | Path, runner: SandboxRunner, *, timeout_sec: int = 30) -> MediaInfo:
    """读取媒体文件的关键参数。"""
    # 解析为绝对路径：runner 会把子进程 cwd 设为 target.parent，
    # 相对路径会被 ffprobe 相对新 cwd 再解析一次而找不到文件。
    target = Path(path).resolve()
    if not target.is_file():
        raise MediaToolError(f"待探测文件不存在：{target}")

    result = runner.run(
        [
            _binary("ffprobe"),
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(target),
        ],
        cwd=target.parent,
        limits=ResourceLimits(timeout_sec=timeout_sec, max_memory_mb=512),
    )
    if not result.ok:
        raise MediaToolError(f"ffprobe 失败：{result.summary()}；{result.tail(500)}")

    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise MediaToolError(f"解析 ffprobe 输出失败：{exc}") from exc

    info = MediaInfo(path=str(target))
    fmt = raw.get("format", {})
    info.duration_sec = _to_float(fmt.get("duration"))
    info.size_bytes = int(_to_float(fmt.get("size")))

    for stream in raw.get("streams", []):
        kind = stream.get("codec_type")
        if kind == "video" and info.width == 0:
            info.width = int(stream.get("width") or 0)
            info.height = int(stream.get("height") or 0)
            info.video_codec = stream.get("codec_name", "")
            info.pix_fmt = stream.get("pix_fmt", "")
            # avg_frame_rate 有时是 "0/0"，回退到 r_frame_rate。
            info.fps = _parse_rational(stream.get("avg_frame_rate")) or _parse_rational(
                stream.get("r_frame_rate")
            )
            if info.duration_sec <= 0:
                info.duration_sec = _to_float(stream.get("duration"))
        elif kind == "audio":
            info.has_audio = True

    return info


def extract_frames(
    video_path: str | Path,
    out_dir: str | Path,
    runner: SandboxRunner,
    *,
    count: int = 4,
    duration_sec: float = 0.0,
    width: int = 1024,
) -> list[str]:
    """从视频中抽帧，供 VLM 审查。

    采样策略（与 Critic 的 rubric 对齐）：
    * 按 ``count`` 等间隔取中点，覆盖整体节奏；
    * **额外加首帧与末帧** —— 入场/收尾的字幕截断、元素溢出最容易出现在这两处，
      而均匀采样常常恰好漏掉它们。

    单帧失败只跳过该帧（不中断），只有一帧都抽不到才抛错。
    """
    target = Path(video_path).resolve()
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    if duration_sec <= 0:
        try:
            duration_sec = probe(target, runner).duration_sec
        except MediaToolError:
            duration_sec = 1.0
    duration_sec = max(duration_sec, 0.1)
    count = max(count, 2)

    timestamps: list[float] = [duration_sec * (i + 0.5) / count for i in range(count)]
    timestamps.append(0.0)
    timestamps.append(max(duration_sec - 0.05, 0.0))

    ffmpeg = _binary("ffmpeg")
    frames: list[str] = []
    for index, ts in enumerate(timestamps):
        frame_path = out / f"frame_{index:02d}.png"
        result = runner.run(
            [
                ffmpeg, "-hide_banner", "-nostdin", "-y",
                # -ss 放在 -i 之前是关键帧快速定位，比解码后再 seek 快得多。
                "-ss", f"{ts:.3f}",
                "-i", str(target),
                "-frames:v", "1",
                # 缩到 1024 宽：足够 VLM 判断字号与排版，又显著降低上传成本。
                "-vf", f"scale={width}:-2",
                str(frame_path),
            ],
            cwd=out,
            limits=ResourceLimits(timeout_sec=60, max_memory_mb=1024),
        )
        if result.ok and frame_path.is_file():
            frames.append(str(frame_path))
        else:
            logger.debug("抽帧失败，已跳过该帧", extra={"ts": ts, "error": result.tail(200)})

    if not frames:
        raise MediaToolError(f"从 {target} 抽帧全部失败，无法进行视觉审查")
    return frames


def render_ambient(
    out_path: str | Path,
    runner: SandboxRunner,
    *,
    duration_sec: float,
    width: int,
    height: int,
    fps: int,
    colors: tuple[str, str, str] = ("0x0B1020", "0x4F8CFF", "0xFF6B6B"),
    text: str = "",
    font_size: int = 64,
    window_start_sec: float = 0.0,
    window_end_sec: float = 0.0,
) -> str:
    """用 ffmpeg 的 lavfi 源渲染一个"氛围"镜头。

    为什么氛围镜头不用 Manim 也不用 headless 浏览器：
    * 它没有可计算内容，只需要一段视觉上舒服的动态背景；
    * lavfi 渲染是**秒级**的，而启动浏览器录帧要数秒、Manim 更是常常几十秒；
    * 依赖最少（只要有 ffmpeg），因此在任何环境下都能跑通 ——
      这使 AMBIENCE 成为整条流水线最可靠的"兜底镜头"。

    文字是**可选装饰**，三级降级：
      1. 找不到中文字体 -> 跳过绘制；
      2. drawtext 滤镜在当前 ffmpeg 构建里不可用 -> **自动去掉文字重试**并缓存该结论；
      3. 仍失败 -> 抛错。
    缺字体/缺滤镜都是环境问题，不该让内容生产停摆。
    """
    target = Path(out_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    font = find_font()
    wants_text = bool(text) and bool(font)
    if text and not font:
        logger.debug("未找到中文字体，氛围镜头将不绘制文字")

    # 已知 drawtext 不可用时直接跳过，避免每个镜头都白跑一次失败的渲染。
    if wants_text and not _drawtext_available():
        logger.debug("本构建的 ffmpeg 不支持 drawtext，跳过文字绘制")
        wants_text = False

    if wants_text:
        result = _run_ambient(target, runner, duration_sec, width, height, fps, colors,
                              font=font, text=text, font_size=font_size,
                              window_start_sec=window_start_sec,
                              window_end_sec=window_end_sec)
        if result.ok and target.is_file():
            return str(target)

        # drawtext 失败 -> 记住结论并**去掉文字重试一次**。
        # 这一步是刻意加的：drawtext 对字体路径转义与 fontconfig 极度敏感，
        # 不同 ffmpeg 构建的行为差异很大（本项目在 Windows 的 gyan 构建上
        # 实际踩到过 fontconfig 缺失导致滤镜初始化失败）。
        # 标题只是装饰，不该因为它让整个镜头渲染不出来。
        _mark_drawtext_unavailable()
        logger.warning(
            "drawtext 渲染失败，改为不绘制文字重试（后续镜头将直接跳过文字）",
            extra={"error": result.tail(300)},
        )

    result = _run_ambient(target, runner, duration_sec, width, height, fps, colors,
                          window_start_sec=window_start_sec,
                          window_end_sec=window_end_sec)
    if not result.ok or not target.is_file():
        raise MediaToolError(f"氛围镜头渲染失败：{result.summary()}；{result.tail(800)}")
    return str(target)


def encode_frames(
    frames_dir: str | Path,
    out_path: str | Path,
    runner: SandboxRunner,
    *,
    fps: int,
    pattern: str = "frame_%05d.png",
    duration_sec: float = 0.0,
) -> str:
    """把 PNG 帧序列编码为 MP4。

    用于 headless 浏览器录帧路径：浏览器逐帧截图 -> 本函数编码。
    相比直接录屏，逐帧截图是**确定性**的（录屏会丢帧、抖动），
    这对"同一份代码产出同一段视频"的可复现性至关重要。
    """
    src = Path(frames_dir).resolve()
    target = Path(out_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    result = runner.run(
        [
            _binary("ffmpeg"), "-hide_banner", "-nostdin", "-y",
            "-framerate", str(fps),
            "-i", str(src / pattern),
            # yuv420p 是最大兼容性的像素格式，缺少它很多播放器会花屏。
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            *(["-t", f"{duration_sec:.3f}"] if duration_sec > 0 else []),
            "-movflags", "+faststart",
            str(target),
        ],
        cwd=target.parent,
        limits=ResourceLimits(timeout_sec=300, max_memory_mb=2048),
    )
    if not result.ok or not target.is_file():
        raise MediaToolError(f"帧序列编码失败：{result.summary()}；{result.tail(800)}")
    return str(target)


# ---------------------------------------------------------------------------
# 氛围镜头的内部实现
# ---------------------------------------------------------------------------

#: 进程内缓存「本构建的 ffmpeg 是否支持 drawtext」。
#:
#: 为什么需要缓存：drawtext 对字体路径转义与 fontconfig 极度敏感，
#: 不同构建行为差异很大（Windows 的 gyan 构建缺 fontconfig 配置会直接失败）。
#: 不缓存的话，每个氛围镜头都要白跑一次失败的渲染才发现这件事。
_DRAWTEXT_STATE: dict[str, bool] = {"probed": False, "available": True}


def _drawtext_available() -> bool:
    return _DRAWTEXT_STATE["available"]


def _mark_drawtext_unavailable() -> None:
    _DRAWTEXT_STATE["available"] = False
    _DRAWTEXT_STATE["probed"] = True


def reset_drawtext_cache() -> None:
    """重置 drawtext 可用性缓存（供测试使用）。"""
    _DRAWTEXT_STATE["available"] = True
    _DRAWTEXT_STATE["probed"] = False


def _build_filtergraph(
    *,
    duration_sec: float,
    width: int,
    height: int,
    fps: int,
    colors: tuple[str, str, str],
    font: str | None = None,
    text: str = "",
    font_size: int = 64,
    window_start_sec: float = 0.0,
    window_end_sec: float = 0.0,
) -> str:
    """构造 lavfi 滤镜图：渐变源（可选叠加 drawtext 标题）。

    局部重渲染（window_start_sec < window_end_sec）时，在链**末尾**追加 trim。

    放在末尾是刻意的：drawtext 的 alpha 表达式依赖 t（原始时间轴），
    若在它之前就 trim，标题的淡入淡出相位会整体前移，
    拼回原片后表现为「标题在错误的时间点亮起」。
    放在末尾则 drawtext 仍按整镜时间轴求值，只有最终输出的帧被裁到窗口内，
    因此编码量按窗口大小下降，而画面内容与整镜渲染完全一致。
    """
    c0, c1, c2 = colors
    filters = [
        f"gradients=s={width}x{height}:c0={c0}:c1={c1}:c2={c2}:n=3"
        f":speed=0.05:d={duration_sec:.3f}:r={fps}"
    ]

    if font and text:
        fade_out_start = max(duration_sec - 0.8, 0)
        filters.append(
            "drawtext="
            f"fontfile={_escape_fontfile(font)}:"
            f"text='{_escape_drawtext(text)}':"
            f"fontcolor=white:fontsize={font_size}:"
            # 居中 + 淡入淡出，让静态标题不至于太生硬。
            "x=(w-text_w)/2:y=(h-text_h)/2:"
            f"alpha='if(lt(t,0.8),t/0.8,if(gt(t,{fade_out_start:.3f}),"
            f"max(0,({duration_sec:.3f}-t)/0.8),1))'"
        )

    if window_end_sec > window_start_sec:
        # setpts 归零是必须的：trim 之后的帧仍带着原始时间戳，
        # 不归零会让编码器把前面的空档也算进去，产出带长空白开头的片段。
        filters.append(
            f"trim=start={window_start_sec:.3f}:end={window_end_sec:.3f},setpts=PTS-STARTPTS"
        )

    return ",".join(filters)


def _run_ambient(
    target: Path,
    runner: SandboxRunner,
    duration_sec: float,
    width: int,
    height: int,
    fps: int,
    colors: tuple[str, str, str],
    *,
    font: str | None = None,
    text: str = "",
    font_size: int = 64,
    window_start_sec: float = 0.0,
    window_end_sec: float = 0.0,
):
    """执行一次 lavfi 渲染。

    注意 duration_sec 始终是**整镜**时长（滤镜图的相位基准），
    局部重渲染时由 window_* 决定实际输出哪一段。
    """
    vf = _build_filtergraph(
        duration_sec=duration_sec, width=width, height=height, fps=fps,
        colors=colors, font=font, text=text, font_size=font_size,
        window_start_sec=window_start_sec, window_end_sec=window_end_sec,
    )
    if window_end_sec > window_start_sec:
        out_duration = window_end_sec - window_start_sec
    else:
        out_duration = duration_sec
    return runner.run(
        [
            _binary("ffmpeg"), "-hide_banner", "-nostdin", "-y",
            "-f", "lavfi", "-i", vf,
            "-t", f"{out_duration:.3f}",
            "-r", str(fps),
            "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-movflags", "+faststart",
            str(target),
        ],
        cwd=target.parent,
        limits=ResourceLimits(
            timeout_sec=max(int(duration_sec * 10) + 60, 90),
            max_memory_mb=2048,
        ),
    )


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _to_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _parse_rational(value: object) -> float:
    """解析 "30000/1001" 形式的有理数帧率。"""
    if not isinstance(value, str) or "/" not in value:
        return _to_float(value)
    num, _, den = value.partition("/")
    d = _to_float(den)
    if d == 0:
        return 0.0
    return _to_float(num) / d


def _escape_fontfile(path: str) -> str:
    """转义 drawtext 的 ``fontfile`` 值 —— 需要**两级**转义。

    ffmpeg 的滤镜图有两次解析：先按「滤镜描述」解析（``\\`` 是转义符），
    再按「选项值」解析（``:`` 分隔选项、``\\`` 是转义符）。
    因此 Windows 路径里的 ``C:`` 必须写成 ``C\\\\:``（字符串里是两个反斜杠）。

    这是**实测**出来的：只写 ``\\:`` 会得到
    ``No option name near '/Windows/Fonts/msyh.ttc:...'`` ——
    第一级解析把反斜杠吃掉后，第二级仍然在冒号处把选项切开。

    注意这里不能用单引号包裹（``fontfile='C:/...'``）：引号分组在
    ``text=`` 上有效，但对 ``fontfile=`` 不生效，实测同样报
    ``No option name``。
    """
    return path.replace("\\", "\\\\").replace(":", "\\\\:")


def _escape_drawtext(text: str) -> str:
    """转义 drawtext 的 ``text`` 值（外层用单引号包裹）。

    实测结论：
    * 单引号包裹对 ``text`` **有效**，其中的中文、全角括号、
      ASCII 冒号都可原样保留，无需额外转义；
    * ``%`` **不需要**转义 —— 裸 ``100%`` 渲染正常。
      只有 ``%{...}`` 这种展开序列才特殊，而标题里几乎不会出现，
      过度转义反而会把反斜杠渲染进画面（早期版本踩过）。
    """
    # 反斜杠要转义，否则会被当作滤镜图的转义符吃掉。
    escaped = text.replace("\\", "\\\\")
    # 单引号是包裹符，替换成同形的全角右单引号，避免破坏引号配对。
    escaped = escaped.replace("'", "\u2019")
    # 换行在单行滤镜里会破坏语法，统一压成空格。
    escaped = escaped.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return escaped

