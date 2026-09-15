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

from ..logging import get_logger
from ..sandbox.runner import ResourceLimits, SandboxRunner

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
) -> str:
    """用 ffmpeg 的 lavfi 源渲染一个"氛围"镜头。

    为什么氛围镜头不用 Manim 也不用 headless 浏览器：
    * 它没有可计算内容，只需要一段视觉上舒服的动态背景；
    * lavfi 渲染是**秒级**的，而启动浏览器录帧要数秒、Manim 更是常常几十秒；
    * 依赖最少（只要有 ffmpeg），因此在任何环境下都能跑通 ——
      这使 AMBIENCE 成为整条流水线最可靠的"兜底镜头"。

    文字是**可选**的：找不到中文字体时自动跳过绘制，
    而不是让整个镜头渲染失败（缺字体是环境问题，不该让内容生产停摆）。
    """
    target = Path(out_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    c0, c1, c2 = colors
    filters = [
        f"gradients=s={width}x{height}:c0={c0}:c1={c1}:c2={c2}:n=3"
        f":speed=0.05:d={duration_sec:.3f}:r={fps}"
    ]

    font = find_font()
    if text and font:
        safe_text = _escape_drawtext(text)
        fade_out_start = max(duration_sec - 0.8, 0)
        filters.append(
            "drawtext="
            f"fontfile='{font}':text='{safe_text}':"
            f"fontcolor=white:fontsize={font_size}:"
            # 居中 + 淡入淡出，让静态标题不至于太生硬。
            "x=(w-text_w)/2:y=(h-text_h)/2:"
            f"alpha='if(lt(t,0.8),t/0.8,if(gt(t,{fade_out_start:.3f}),"
            f"max(0,({duration_sec:.3f}-t)/0.8),1))'"
        )
    elif text:
        logger.debug("未找到中文字体，氛围镜头将不绘制文字")

    vf = ",".join(filters)

    result = runner.run(
        [
            _binary("ffmpeg"), "-hide_banner", "-nostdin", "-y",
            "-f", "lavfi", "-i", vf,
            "-t", f"{duration_sec:.3f}",
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


def _escape_drawtext(text: str) -> str:
    """转义 drawtext 的特殊字符。

    drawtext 的 filter 语法里 ``:`` 分隔参数、``'`` 包裹字符串、
    ``%`` 触发时间格式展开、``\\`` 是转义符。不转义会直接让 filter 解析失败。
    """
    escaped = text.replace("\\", "\\\\")
    escaped = escaped.replace(":", "\\:").replace("'", "\u2019")
    escaped = escaped.replace("%", "\\%")
    # 换行在单行 filter 里必须转成字面量 \n
    escaped = escaped.replace("\n", "\\n")
    return escaped
