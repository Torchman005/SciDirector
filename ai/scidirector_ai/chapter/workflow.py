"""章节工作流：视频内容 → 脚本 → 旁白时间轴 → 场景动画 → 预检修复 → 成片。"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..llm import LLMClient
from ..tts.base import TTSProvider
from .compile import compile_chapter, preflight_chapter, render_chapter
from .narration import estimate_timing, synthesize_chapter
from .writer import ChapterScript, write_scene, write_script

logger = logging.getLogger(__name__)

#: 每个场景最多的预检修复轮数。
MAX_REPAIR_ROUNDS = 3
_SCENE_TAG = re.compile(r"^\[场景 ([A-Za-z][A-Za-z0-9_]*)\]")
#: 这些问题会让场景画面缺失，修不好就不能出片；版式问题修不好只警告。
_FATAL = ("建立失败", "更新失败", "未知旁白", "旁白 ", "页面异常")


@dataclass
class ChapterResult:
    script: ChapterScript
    timing: dict
    html: Path
    video: Path | None = None
    warnings: list[str] = field(default_factory=list)


def _assemble(codes: list[str]) -> str:
    return "\n\n".join(codes)


def _tagged(script: ChapterScript, codes: list[str]) -> list[tuple[str, str]]:
    return [(scene.lines[0].id, code) for scene, code in zip(script.scenes, codes)]


def _by_scene(problems: list[str], script: ChapterScript) -> dict[str, list[str]]:
    first_ids = {s.lines[0].id: i for i, s in enumerate(script.scenes)}
    out: dict[str, list[str]] = {}
    for problem in problems:
        match = _SCENE_TAG.match(problem)
        key = match.group(1) if match and match.group(1) in first_ids else "*"
        out.setdefault(key, []).append(problem)
    return out


def make_chapter(llm: LLMClient, content: str, out_dir: str | Path, *, tts: TTSProvider | None,
                 target_sec: int = 90, style: str = "", executable: str = "", render: bool = True,
                 script: ChapterScript | None = None, tts_attempts: int = 3) -> ChapterResult:
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    if script is None:
        script = write_script(llm, content, target_sec=target_sec, style=style)
    (out / "script.json").write_text(script.model_dump_json(indent=1), encoding="utf-8")
    logger.info("章节脚本完成", extra={"scenes": len(script.scenes),
                                     "lines": sum(len(s.lines) for s in script.scenes)})

    audio = None
    if tts is None:
        timing = estimate_timing(script.chapter_lines())
        (out / "timing.json").write_text(json.dumps(timing, ensure_ascii=False, indent=1), encoding="utf-8")
    else:
        audio, timing = synthesize_chapter(tts, script.chapter_lines(), out, attempts=tts_attempts)

    codes = [write_scene(llm, script, i, timing) for i in range(len(script.scenes))]
    html = out / "index.html"
    warnings: list[str] = []
    for round_ in range(MAX_REPAIR_ROUNDS + 1):
        (out / "director.js").write_text(_assemble(codes), encoding="utf-8")
        html.write_text(compile_chapter(_tagged(script, codes), timing, series=script.series, title=script.title,
                                        highlights=script.highlights), encoding="utf-8")
        problems = preflight_chapter(html, timing, executable=executable)
        logger.info("章节预检", extra={"round": round_, "problems": len(problems)})
        if not problems:
            warnings = []
            break
        grouped = _by_scene(problems, script)
        if round_ == MAX_REPAIR_ROUNDS:
            fatal = [p for p in problems if any(k in p for k in _FATAL)]
            if fatal:
                raise RuntimeError("场景代码多轮修复后仍有错误：\n" + "\n".join(fatal[:10]))
            warnings = problems
            break
        for index, scene in enumerate(script.scenes):
            own = grouped.get(scene.lines[0].id, []) + grouped.get("*", [])
            if own:
                codes[index] = write_scene(llm, script, index, timing, previous_code=codes[index], problems=own)

    result = ChapterResult(script=script, timing=timing, html=html, warnings=warnings)
    if render:
        result.video = render_chapter(html, timing, out, audio=audio, executable=executable)
    return result
