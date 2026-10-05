"""Real browser negative controls for the production sandbox preflight."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scidirector_ai import generation_quality as quality
from scidirector_ai.config import Settings, reset_browser_probe_cache
from scidirector_ai.generation_quality import BrowserPreflight
from scidirector_ai.rag import load_corpus
from scidirector_ai.schemas import SceneTag, ShotSpec, StyleGuide


def test_entry_point_imports_without_shadowing_logging() -> None:
    script = Path(quality.__file__).with_name("browser_preflight.py")
    result = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def checker(tmp_path: Path) -> BrowserPreflight:
    chrome = os.environ.get("SCID_CHROME", "")
    if not chrome or not Path(chrome).is_file():
        pytest.skip("设置 SCID_CHROME 后运行真实浏览器预检对照")
    reset_browser_probe_cache()
    settings = Settings(env="test", llm_provider="openai", sandbox_work_dir=str(tmp_path),
                        render_width=320, render_height=180, sandbox_network_isolation="off",
                        sandbox_read_only="off", sandbox_seccomp="off")
    return BrowserPreflight(settings)


GOOD = """<div id='label' style='position:absolute;left:60px;top:60px;font-size:32px;color:white'>Readable</div>
<script>window.__seek=t=>{document.getElementById('label').style.left=(60+t*8)+'px'};window.__ready=true;</script>"""


@pytest.mark.parametrize("code,needle", [
    (GOOD, ""), (GOOD.replace("32px", "12px"), "字号"),
    (GOOD.replace("(60+t*8)", "(290+t*8)"), "裁切"),
    (GOOD.replace("t=>{document", "t=>{missingSymbol();document"), "执行失败"),
    (GOOD.replace("(60+t*8)", "(60+(window.n=(window.n||0)+12))"), "seek"),
    ("<div>Only comment</div><script>// window.__seek(t)</script>", "运行时不存在"),
    ("<div style='font-size:32px'><span style='display:none;font-size:12px'>Hidden</span>Readable</div><script>window.__seek=t=>{};</script>", ""),
])
def test_browser_catches_defects_and_accepts_controls(checker: BrowserPreflight, code: str, needle: str) -> None:
    result = checker.check(code, ShotSpec(tag=SceneTag.MOTION, duration_sec=2), StyleGuide(min_font_size=32))
    assert result.checked, result.reason
    if needle:
        assert any(needle in issue for issue in result.issues), result
    else:
        assert not result.issues, result


def test_clipping_parent_and_svg_transform_are_measured(checker: BrowserPreflight) -> None:
    clipped = """<div style='width:50px;white-space:nowrap;overflow:hidden'><span style='font-size:32px'>Long text</span></div><script>window.__seek=t=>{};</script>"""
    svg = """<svg width='320' height='180' viewBox='0 0 640 360'><text x='80' y='120' font-size='32' fill='white'>Small</text></svg><script>window.__seek=t=>{};</script>"""
    for code, needle in [(clipped, "裁切"), (svg, "字号")]:
        result = checker.check(code, ShotSpec(tag=SceneTag.MOTION, duration_sec=2), StyleGuide(min_font_size=32))
        assert result.checked, result.reason
        assert any(needle in issue for issue in result.issues), result


def test_updated_ambient_example_has_real_readable_output(checker: BrowserPreflight) -> None:
    checker.settings.render_width, checker.settings.render_height = 1920, 1080
    example = next(e for e in load_corpus() if e.id == "ambience-001")
    result = checker.check(example.code, ShotSpec(tag=SceneTag.AMBIENCE, duration_sec=8), StyleGuide(min_font_size=32))
    assert result.checked, result.reason
    assert not result.issues, result
