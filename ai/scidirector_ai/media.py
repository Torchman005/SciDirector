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
    * 返回的帧**严格按时间升序**，且首帧取一个极小偏移而非 0.0：
      审查提示词声明「图像按时间先后排列，第一张是首帧、最后一张是末帧」，
      抽取顺序一旦与这句话不符，VLM 会把顺序错乱读成「动画顺序有问题」；
      而 t=0 对以 `Create`/`Write` 开场的镜头本来就是空帧，
      会被 rubric 的「画面几乎全空」当成致命问题。两处理由见函数内注释。

    单帧失败只跳过该帧（不中断），只有一帧都抽不到才抛错。
    """
    target = Path(video_path).resolve()
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    samples = extract_frames_with_times(
        target, out, runner,
        count=count, duration_sec=duration_sec, width=width,
    )
    return [s.path for s in samples]


@dataclass(frozen=True)
class FrameSample:
    """一帧采样：图片路径 + 它对应的**时间点**。

    时间点必须跟着帧一起传下去：节奏检查要回答的是
    "第几秒到第几秒画面没有变化"，没有时间点就只能说"有两帧一样" ——
    那对编码端毫无指导意义。
    """

    path: str
    ts: float


def frame_sample_times(duration_sec: float, count: int) -> list[float]:
    """计算抽帧的时间点（升序、去重）。

    **这是唯一的一份实现。** `extract_frames_with_times` 用它决定抽哪几帧，
    `analyze_motion` 用它把"相邻两帧"翻译成"第几秒到第几秒"。
    两处各算一遍的后果不是"稍微不准"，而是**反馈里的秒数与实际抽帧对不上** ——
    编码端会照着错误的时段去改画面，而且改完还是不动。

    采样策略（与 Critic 的 rubric 对齐）见 `extract_frames` 的文档。
    """
    duration_sec = max(duration_sec, 0.1)
    count = max(count, 2)

    # 采样点必须**按时间排序**后再抽帧。
    #
    # 这一点曾经是错的，代价很大：这里过去直接产出
    # `[中点采样..., 0.0, 末帧]`，实际时间顺序成了
    # 「12.5% → 37.5% → 62.5% → 87.5% → 0% → 100%」。
    # 而审查提示词明确告诉 VLM「图像按时间先后排列，第一张是首帧、
    # 最后一张是末帧」—— 于是它看到的是「画面齐全 → 突然全空 → 又齐全」，
    # **合理地**判定「动画顺序有问题」，连续多轮给出同一条建议，
    # 整条重试链白烧（实测 0.65 → 0.59，三次 attempt 全部浪费）。
    # 教训：喂给模型的样本，其**语义说明必须与实际排列一致**；
    # 顺序错了比内容错了更隐蔽，因为画面每一张看起来都正常。
    #
    # 首帧不取 0.0，而取一个小偏移：以 `Create` / `Write` 开场的镜头在 t=0 时
    # 第一个动画的进度是 0，画面**本来就是空的**，取在那里必然得到一张空白帧，
    # 而 rubric 把「画面几乎全空」列为致命问题 —— 那是对正常镜头的误杀。
    # 取 min(0.2s, 1/4 个采样间隔) 既能代表入场状态，又不会落在空帧上。
    first_ts = min(0.2, duration_sec / (count * 4))
    timestamps: list[float] = [duration_sec * (i + 0.5) / count for i in range(count)]
    timestamps.append(first_ts)
    timestamps.append(max(duration_sec - 0.05, 0.0))

    # 排序 + 去重：极短的视频里中点可能与首/末帧落到同一时刻，
    # 重复的采样点会把同一张图喂两遍，白白抬高上传成本。
    ordered: list[float] = []
    seen: set[str] = set()
    for ts in sorted(timestamps):
        key = f"{ts:.3f}"
        if key not in seen:
            seen.add(key)
            ordered.append(ts)
    return ordered


def extract_frames_with_times(
    video_path: str | Path,
    out_dir: str | Path,
    runner: SandboxRunner,
    *,
    count: int = 4,
    duration_sec: float = 0.0,
    width: int = 1024,
) -> list[FrameSample]:
    """与 :func:`extract_frames` 相同，但每帧**带上时间点**。"""
    target = Path(video_path).resolve()
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    if duration_sec <= 0:
        try:
            duration_sec = probe(target, runner).duration_sec
        except MediaToolError:
            duration_sec = 1.0

    ordered = frame_sample_times(duration_sec, count)

    ffmpeg = _binary("ffmpeg")
    samples: list[FrameSample] = []
    for index, ts in enumerate(ordered):
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
            samples.append(FrameSample(path=str(frame_path), ts=ts))
        else:
            logger.debug("抽帧失败，已跳过该帧", extra={"ts": ts, "error": result.tail(200)})

    if not samples:
        raise MediaToolError(f"从 {target} 抽帧全部失败，无法进行视觉审查")
    return samples


#: 判定"两个像素算不算变了"的灰阶差阈值。
#:
#: 取 20 而不是 1~2：视频有编码噪声，静止画面的相邻帧也会有 1~2 个灰阶的抖动。
#: 用 1 做阈值的话每一帧都"有变化"，节奏检查会彻底失效。
_CHANGE_GRAY_LEVEL = 20

#: 判定"这一段画面基本没动"的变化像素占比下限（0.5%）。
#:
#: 实测数据支撑：一条 22.83 秒真实镜头里，静止时段的占比是
#: `0.19% / 0.12% / 0.06%`，而有明显变化的时段是 `4.45% / 11.51%`——
#: 两者相差一个数量级，0.5% 落在中间，不会误报也不会漏报。
#: 注意 1~2% 属于"只有局部在动"（例如角落图标），**不算**静止。
_DEFAULT_MIN_CHANGE_RATIO = 0.005

#: 太短的时间间隔不参与判定（秒）。
#:
#: 相邻采样点之间若只隔了零点几秒，画面变化本来就可能很小，
#: 那是采样密度问题而不是"画面不动"。
_DEFAULT_MIN_SPAN_SEC = 1.0


@dataclass
class MotionReport:
    """对抽帧序列做的**可计算**节奏检查结果。"""

    #: 每个相邻采样对的变化像素占比（0~1），长度 = 采样数 - 1。
    ratios: list[float]
    #: 被判定为"基本静止"的时段：(起, 止, 变化占比)。
    static_spans: list[tuple[float, float, float]]
    #: 判定用到的下限，写进反馈里让编码端知道目标。
    min_change_ratio: float

    @property
    def ok(self) -> bool:
        return not self.static_spans

    def summary(self) -> str:
        """给编码智能体的**带时间点**的反馈。

        这是整个检查的意义所在：把"节奏不好"这种观感，
        换成"第 13.3 秒到第 17.1 秒画面没有变化"这种**可核对的事实**。
        实测反馈里写"把打字 run_time 从 0.5 延长到 3 秒"时，
        编码端照做了、画面却依然静止 —— 因为它不知道该填哪一段时间。
        """
        if self.ok:
            return ""
        best = max(self.ratios) if self.ratios else 0.0
        lines = [
            "【画面变化的量化测量（系统计算，不是观感）】",
            "以下时段里画面**几乎没有任何变化**（相邻抽帧的变化像素占比低于 "
            f"{self.min_change_ratio * 100:.1f}%）：",
        ]
        for start, end, ratio in self.static_spans:
            lines.append(f"- 第 {start:.1f} 秒 → 第 {end:.1f} 秒（变化像素占比 {ratio * 100:.2f}%）")
        lines.append(
            f"参考：本镜头变化最明显的时段占比为 {best * 100:.1f}%。"
            "这些时段画面变化很小，但不能据此断言整帧完全一致。"
        )
        lines.append(
            "请针对**上面这些具体时段**安排可见的变化：把内容拆成阶段让动作延续到那些时刻，"
            "而不是延长某个已有动画的时长（延长时间填不满这些空档）。"
        )
        return "\n".join(lines)


def analyze_motion(
    samples: list[FrameSample],
    *,
    min_change_ratio: float = _DEFAULT_MIN_CHANGE_RATIO,
    min_span_sec: float = _DEFAULT_MIN_SPAN_SEC,
    ignore_last_interval: bool = True,
) -> MotionReport:
    """检查画面是否**贯穿整段时长**都在变化。

    为什么需要（这是"重试几次问题和建议都不变"的根因）：
    实测一条 22.83 秒的镜头，动画演到约 30% 处就基本静止，
    之后的变化像素占比一路是 `0.19% / 0.12% / 0.06%`。
    审查连续四轮判它"动画停滞"——**判断是对的**，但每轮给的都是
    "把打字 run_time 延长到 3 秒"这类微调，量级上填不满十几秒，
    于是代码改来改去画面不变、审查结论一字不差，一直烧到人工介入。

    VLM 看几张静帧只能得出"感觉没怎么动"；而这个检查是**算出来的**，
    能直接告诉编码端"第 13.3 秒到第 17.1 秒没有变化"。

    ``ignore_last_interval``：最后一个间隔不判定。
    提示词允许收尾处停住（"结束前留 0.5 秒静止"、最终完成态在最后到位），
    把那里也判成缺陷会逼模型在结尾硬塞动作。
    """
    ratios: list[float] = []
    spans: list[tuple[float, float, float]] = []
    last_index = len(samples) - 2
    for i in range(len(samples) - 1):
        prev, cur = samples[i], samples[i + 1]
        ratio = _frame_change_ratio(prev.path, cur.path)
        ratios.append(ratio)
        if ignore_last_interval and i == last_index:
            continue
        span = cur.ts - prev.ts
        if span < min_span_sec:
            continue
        if ratio < min_change_ratio:
            spans.append((prev.ts, cur.ts, ratio))
    return MotionReport(ratios=ratios, static_spans=spans, min_change_ratio=min_change_ratio)


def _frame_change_ratio(prev_path: str, cur_path: str) -> float:
    """两帧之间"变化幅度超过阈值"的像素占比（0~1）。

    用**占比**而不是全画面平均差：平均差会被"大面积但幅度小"的变化主导，
    而"小面积但幅度大"的变化（角落图标在转、指示灯在闪）会被抹平 ——
    实测一个 22.83 秒镜头里角落风扇的旋转在平均差上只有 0.2~0.3，
    看起来就像静止。占比对这个量级更敏感，也更容易设阈值。
    """
    from PIL import Image, ImageChops

    with Image.open(prev_path) as a_img, Image.open(cur_path) as b_img:
        a = a_img.convert("L")
        b = b_img.convert("L")
        if a.size != b.size:
            b = b.resize(a.size)
        hist = ImageChops.difference(a, b).histogram()
    total = sum(hist) or 1
    return sum(hist[_CHANGE_GRAY_LEVEL + 1 :]) / total


def frame_change_summary(paths: list[str]) -> str:
    """Give the critic measurable adjacent-frame evidence without inferring motion."""
    if len(paths) < 2:
        return "只有一张抽帧，无法比较相邻画面。"
    lines = []
    for index, (previous, current) in enumerate(zip(paths, paths[1:]), start=1):
        ratio = _frame_change_ratio(previous, current)
        lines.append(f"第 {index}、{index + 1} 帧：变化像素占比 {ratio * 100:.2f}%")
    return "\n".join(lines)


#: 氛围镜头标题字号相对输出高度的比例：标题约占画面高度的 1/12。
_AMBIENT_TITLE_HEIGHT_RATIO = 12


def ambient_font_size(height: int) -> int:
    """按输出高度推算氛围镜头的标题字号。

    **不能写死一个像素值**：64px 在 320x240 的年代是醒目的大标题，
    到了 1920x1080 就只是个不起眼的小注脚 —— 实测审查智能体在 1080p 下
    据此连续判「标题字号不足」。按高度取比例在两端都合适
    （240p -> 20px，1080p -> 90px），也与「标题约占画面高度 1/12」的常识一致。

    下限 16px 是给极小的草稿分辨率兜底，避免字号退化成看不清的个位数。
    """
    return max(16, round(height / _AMBIENT_TITLE_HEIGHT_RATIO))


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
    font_size: int = 0,
    window_start_sec: float = 0.0,
    window_end_sec: float = 0.0,
) -> str:
    """用 ffmpeg 的 lavfi 源渲染一个"氛围"镜头。

    为什么氛围镜头不用 Manim 也不用 headless 浏览器：
    * 它没有可计算内容，只需要一段视觉上舒服的动态背景；
    * lavfi 渲染是**秒级**的，而启动浏览器录帧要数秒、Manim 更是常常几十秒；
    * 依赖最少（只要有 ffmpeg），因此在任何环境下都能跑通 ——
      **浏览器不可用时，环境镜头会降级到这条路径**（见 ``graph/nodes.py``
      的 ``_engine_or_fallback``）。注意环境镜头的**默认**引擎已经是
      HTML 动效，这条路是兜底而非首选。

    文字是**可选装饰**，三级降级：
      1. 找不到中文字体 -> 跳过绘制；
      2. drawtext 滤镜在当前 ffmpeg 构建里不可用 -> **自动去掉文字重试**并缓存该结论；
      3. 仍失败 -> 抛错。
    缺字体/缺滤镜都是环境问题，不该让内容生产停摆。

    ``font_size <= 0`` 表示**按输出高度自动推算**（见 :func:`ambient_font_size`），
    这也是缺省值：字号与分辨率绑死会在某一端必然出错。
    """
    font_size = font_size or ambient_font_size(height)

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
        # 描边宽度随字号缩放 —— 大字号下细描边等于没加。
        border_w = max(2, round(font_size / 16))
        filters.append(
            "drawtext="
            f"fontfile={_escape_fontfile(font)}:"
            f"text='{_escape_drawtext(text)}':"
            f"fontcolor=white:fontsize={font_size}:"
            # 深色描边 + 阴影：标题是白字，而渐变底色的中段是**明亮的蓝色**，
            # 纯白字压在上面实测被判「对比度不足」。给字加一圈深色描边是标准做法，
            # 而且对任何背景色都成立 —— 比反复调背景色更稳，也不牺牲渐变的观感。
            f"borderw={border_w}:bordercolor=black@0.75:"
            "shadowcolor=black@0.5:shadowx=2:shadowy=2:"
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

