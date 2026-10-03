"""逐帧截图（`html_capture.py`）的契约与不变式测试。

这里刻意**不 import 那个模块**：它在导入时会改写本进程的 `sys.path`
（为的是摆脱同目录 `logging.py` 的遮蔽），一旦在 pytest 进程里执行，
后续的 `import logging` 类行为就可能被带偏 —— 测试不该有这种副作用。
因此凡是需要它内部常量的地方，都起一个**子进程**去问。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scidirector_ai import renderer

CAPTURE = Path(renderer.__file__).with_name("html_capture.py")


def _probe(expr: str) -> str:
    """在子进程里按路径加载 html_capture 并求值。"""
    code = (
        "import importlib.util, json, pathlib;"
        f"p = pathlib.Path(r'{CAPTURE}');"
        "spec = importlib.util.spec_from_file_location('hc_probe', p);"
        "m = importlib.util.module_from_spec(spec);"
        "spec.loader.exec_module(m);"
        f"print(json.dumps({expr}))"
    )
    res = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    if res.returncode != 0:
        raise AssertionError(f"子进程加载 html_capture 失败：{res.stderr[-800:]}")
    return json.loads(res.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# 导入顺序：一个把「报错指向错误方向」的陷阱钉死
# ---------------------------------------------------------------------------


def test_script_imports_cleanly() -> None:
    """脚本必须能干净导入 —— 用 `--help` 走完整条导入链。

    守的是一个**很难从报错反推**的缺陷：本脚本所在目录里有一个本项目自己的
    `logging.py`，只要它在 `sys.path` 上，任何 `import logging`（`asyncio`、
    `concurrent.futures`、playwright 都会）就会拿到我们的模块，然后报
    `module 'logging' has no attribute 'Formatter'` —— 看起来像 playwright 坏了
    或依赖没装，实际是路径遮蔽。

    所以脚本里那段 `sys.path` 清理必须发生在**第一个会间接导入 logging 的模块之前**。
    v0.6.15 给它加 `import asyncio` 时正是踩了这个坑。这条用例会在那种改动下立刻变红。
    """
    res = subprocess.run(
        [sys.executable, str(CAPTURE), "--help"], capture_output=True, text=True, timeout=120
    )
    assert res.returncode == 0, f"--help 都跑不起来，导入链有问题：{res.stderr[-800:]}"
    assert "usage" in res.stdout
    assert "has no attribute 'Formatter'" not in (res.stdout + res.stderr)


# ---------------------------------------------------------------------------
# 帧号切分：成片是否正确全靠它
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("total", [0, 1, 2, 7, 30, 240, 241])
@pytest.mark.parametrize("workers", [1, 2, 3, 8, 16, 64])
def test_split_frames_covers_every_index_exactly_once(total: int, workers: int) -> None:
    """各段必须**不重叠、并集恰好是 [0,total)、且保持升序**。

    这条不变式一旦破坏，表现是"成片缺帧或顺序错乱"，而 ffmpeg 对缺帧**不会报错** ——
    它只会静默产出一个更短的视频。所以必须在这里钉住，而不是等到看成片。
    """
    chunks = _probe(f"m.split_frames({total}, {workers})")
    flat = [i for c in chunks for i in c]
    assert flat == list(range(total)), f"覆盖不正确：{chunks}"
    for chunk in chunks:
        assert chunk == sorted(chunk), "段内必须升序"
        assert chunk, "不允许空段（空段会白白多起一个浏览器页面）"


def test_split_frames_respects_worker_cap() -> None:
    """并发页面数不能超过帧数 —— 多出来的页面会空转。"""
    assert len(_probe("m.split_frames(3, 8)")) == 3
    assert _probe("m.split_frames(0, 8)") == []


def test_default_workers_is_bounded() -> None:
    """默认并发有上限：单镜头渲染本来就与别的镜头并行，不该把整机吃满。"""
    workers = _probe("m.DEFAULT_WORKERS")
    assert 1 <= workers <= 8


# ---------------------------------------------------------------------------
# 格式与文件名：两处必须一致，否则 ffmpeg 读不到帧序列
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["jpeg", "png"])
def test_frame_name_matches_renderer_pattern(fmt: str) -> None:
    """截图脚本产出的文件名，必须能被 `encode_frames` 的 pattern 匹配上。

    这两处分别在两个文件里（截图在沙盒子进程里跑，不能 import 本项目的包，
    所以没法共享常量）。一旦漂移，ffmpeg 会因为找不到帧序列而失败 ——
    是**响亮地**失败（好过静默出错片），但也不该等到那时候才发现。
    """
    assert _probe(f"m.frame_name(7, '{fmt}')") == renderer._FRAME_PATTERN[fmt] % 7


def test_renderer_uses_the_fast_path_by_default() -> None:
    """默认必须是 JPEG —— 这条改动本身就是性能修复，别被"改回 PNG 更保险"改掉。

    实测（240 帧 @1920x1080，32 核）：PNG×1 页面 99.6s → JPEG×8 页面 12.5s。
    """
    assert renderer._HTML_FRAME_FORMAT == "jpeg"
    assert 1 <= renderer._HTML_FRAME_QUALITY <= 100
    assert "jpeg" in renderer._FRAME_PATTERN
