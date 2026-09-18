"""TTS 服务商适配层。

对外只暴露协议与工厂，具体服务商按需导入（避免为了用 Edge 而要求装上所有 SDK）。
"""

from __future__ import annotations

from .base import (
    MARKS_FORMAT_VERSION,
    SentenceMark,
    SynthesisResult,
    TTSError,
    TTSProvider,
    marks_sidecar_path,
    read_marks_sidecar,
    write_marks_sidecar,
)

__all__ = [
    "MARKS_FORMAT_VERSION",
    "SentenceMark",
    "SynthesisResult",
    "TTSError",
    "TTSProvider",
    "marks_sidecar_path",
    "read_marks_sidecar",
    "write_marks_sidecar",
]
