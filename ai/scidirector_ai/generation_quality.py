"""Cheap generation gates and a bounded sandbox browser preflight before full video rendering."""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .config import Settings
from .renderer import RenderRequest, _wrap_html
from .sandbox.policy import PolicyReport, PolicyViolation
from .sandbox.runner import ResourceLimits, SandboxRunner
from .schemas import ShotSpec, StyleGuide

# Brief budgets use actual pixels, not invented VLM estimates.
SAFE_MARGIN_RATIO = .06
TARGET_STAGE_SEC = 4.0
PREFLIGHT_TIMEOUT_SEC = 30
PREFLIGHT_FAILURE_COOLDOWN_SEC = 60


def production_brief(shot: ShotSpec, style: StyleGuide, settings: Settings) -> str:
    width, height = settings.render_width, settings.render_height
    left, top = round(width * SAFE_MARGIN_RATIO), round(height * SAFE_MARGIN_RATIO)
    beats = shot.beats or [shot.visual_brief or shot.narration or "展示主题"]
    # A complex brief shouldn't become dozens of tiny stages; supplied beats remain visible.
    lines = [f"实际画布 {width}×{height}px；字号下限 {style.min_font_size}px。",
             f"内容安全区 x={left}～{width-left}，y={top}～{height-top}；按实际文字宽度分配主图与说明。",
             f"视频播放时长 {shot.duration_sec:.2f}s；这不是进程执行超时，沙盒执行上限为 {settings.sandbox_timeout_sec}s。",
             "按以下旁白相关节拍分配时间（同等分配是起点，可依据句长调整；总时长保持一致）："]
    for i, beat in enumerate(beats):
        start, end = shot.duration_sec * i / len(beats), shot.duration_sec * (i + 1) / len(beats)
        lines.append(f"- {start:.2f}～{end:.2f}s：{beat}")
    if not shot.beats and shot.duration_sec > TARGET_STAGE_SEC:
        lines.append(f"尚无导演节拍；把主题按旁白拆成约 {max(2, round(shot.duration_sec / TARGET_STAGE_SEC))} 个可见讲解步骤，避免开场演完后长时间等待。")
    if shot.engine and shot.engine.value == "manim":
        lines.append("Manim 使用场景坐标，以上像素安全区换算为 config.frame_width/frame_height 的 6% 边距；字号按成片可读效果判断。")
    return "\n".join(lines)


def check_manim_structure(code: str) -> PolicyReport:
    report = PolicyReport()
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return report  # Safety gate already supplies the exact syntax diagnostic.
    scenes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SciShotScene"]
    construct = next((node for scene in scenes for node in scene.body
                      if isinstance(node, ast.FunctionDef) and node.name == "construct"), None)
    if construct is None:
        report.violations.append(PolicyViolation(reason="缺少 SciShotScene.construct",
            advice="必须定义 class SciShotScene(Scene) 及 construct(self)，在其中创建并显示场景对象。"))
    elif not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and isinstance(node.func.value, ast.Name) and node.func.value.id == "self"
                 and node.func.attr in {"play", "add", "add_fixed_in_frame_mobjects"}
                 for node in ast.walk(construct)):
        report.violations.append(PolicyViolation(reason="场景未显示任何对象", lineno=construct.lineno,
            advice="construct 必须调用 self.add 或 self.play 显示实际图示，不能只声明对象或等待。"))
    return report


@dataclass
class QualityResult:
    checked: bool = False
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reason: str = ""


class QualityChecker(Protocol):
    def check(self, code: str, shot: ShotSpec, style: StyleGuide) -> QualityResult: ...


class BrowserPreflight:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.runner = SandboxRunner(settings.sandbox_network_isolation,
                                    settings.sandbox_read_only, settings.sandbox_seccomp)
        self._failure_lock = threading.Lock()
        self._failure_until = 0.0
        self._failure_reason = ""

    def _unavailable(self, reason: str) -> QualityResult:
        # A broken installation should not add a fresh timeout to every shot.
        with self._failure_lock:
            self._failure_reason = reason
            self._failure_until = time.monotonic() + PREFLIGHT_FAILURE_COOLDOWN_SEC
        return QualityResult(reason=reason)

    def check(self, code: str, shot: ShotSpec, style: StyleGuide) -> QualityResult:
        if not self.settings.coder_preflight_enabled or self.settings.text_target().provider == "mock":
            return QualityResult(reason="预检关闭或整体 mock 模式")
        if not shot.engine or shot.engine.value == "manim":
            return QualityResult(reason="Manim 使用静态结构校验；浏览器预检不适用")
        with self._failure_lock:
            if time.monotonic() < self._failure_until:
                return QualityResult(reason="预检环境故障冷却中：" + self._failure_reason)
        # Playwright discovery itself starts a driver. Keep *all* browser work within
        # the sandbox timeout so a broken installation cannot stall generation.
        work = Path(self.settings.sandbox_work_dir).resolve() / "coder_preflight"
        try:
            work.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="shot-", dir=work) as temp:
                root = Path(temp)
                html = root / "index.html"
                html.write_text(_wrap_html(RenderRequest(shot_id=shot.shot_id, code=code, output_dir=root,
                    duration_sec=shot.duration_sec, width=self.settings.render_width,
                    height=self.settings.render_height, fps=self.settings.render_fps,
                    background_color=style.background_color, primary_color=style.primary_color)), encoding="utf-8")
                output = root / "quality.json"
                argv = [sys.executable, str(Path(__file__).with_name("browser_preflight.py")),
                        "--html", str(html), "--output", str(output),
                        "--width", str(self.settings.render_width), "--height", str(self.settings.render_height),
                        "--duration", str(shot.duration_sec), "--min-font", str(style.min_font_size)]
                if chrome := os.environ.get("SCID_CHROME"):
                    argv += ["--executable", chrome]
                result = self.runner.run(argv, cwd=root,
                                         limits=ResourceLimits(timeout_sec=PREFLIGHT_TIMEOUT_SEC))
                if not result.ok or not output.is_file():
                    return self._unavailable(f"预检未完成：{result.summary()} {result.tail(300)}")
                payload = json.loads(output.read_text(encoding="utf-8"))
                return QualityResult(checked=True, issues=payload["issues"], warnings=payload["warnings"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return self._unavailable(f"预检未完成：{type(exc).__name__}: {str(exc)[:200]}")
