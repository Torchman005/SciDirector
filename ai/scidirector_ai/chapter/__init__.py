"""章节模式：整章旁白先行，画面以旁白逐字时刻为锚点（见 docs/CHAPTER_MODE.md）。"""
from .compile import compile_chapter, preflight_chapter, render_chapter
from .narration import ChapterLine, estimate_timing, synthesize_chapter

__all__ = ["ChapterLine", "compile_chapter", "estimate_timing", "preflight_chapter",
           "render_chapter", "synthesize_chapter"]
