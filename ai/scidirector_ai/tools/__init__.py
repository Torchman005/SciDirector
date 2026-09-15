"""媒体与渲染工具。

    media       ffmpeg / ffprobe 的底层调用（探测、抽帧、氛围镜头、帧序列编码）
    renderer    按「标签 -> 引擎」的确定性路由调度具体渲染实现

阶段一仅有沙盒静态策略；本包是阶段二引入的渲染执行层。
"""

from .media import (
    MediaInfo,
    MediaToolError,
    encode_frames,
    extract_frames,
    find_font,
    probe,
    render_ambient,
)

__all__ = [
    "MediaInfo",
    "MediaToolError",
    "encode_frames",
    "extract_frames",
    "find_font",
    "probe",
    "render_ambient",
]
