"""Live2D is rendered as an alpha layer; Go retains ownership of final composition."""
from __future__ import annotations

import json
import math
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from .config import Settings
from .sandbox.runner import ResourceLimits, SandboxRunner


def runtime_paths(settings: Settings) -> dict[str, str]:
    root = Path(settings.live2d_runtime_dir).resolve() if settings.live2d_runtime_dir else Path(__file__).parents[1] / "live2d-runtime/node_modules"
    paths = {"core": Path(settings.live2d_core_path).expanduser() if settings.live2d_core_path else None,
             "pixi": root / "pixi.js/dist/browser/pixi.min.js",
             "display": root / "pixi-live2d-display/dist/cubism4.min.js"}
    for name, path in paths.items():
        if path is None or not path.is_file():
            raise ValueError(f"Live2D 运行库 {name} 未就绪；安装 ai/live2d-runtime 依赖并设置 SCID_LIVE2D_CORE_PATH（官方 Cubism Core）")
    return {name: str(path.resolve()) for name, path in paths.items()}


def render_presenter(settings: Settings, runner: SandboxRunner, *, model_path: str, audio_path: str,
                     output_dir: str, duration_sec: float, width: int, height: int, fps: int,
                     mouth_parameter: str = "", mouth_gain: float = 1,
                     timeout_sec: float | None = None) -> dict[str, Any]:
    libraries = runtime_paths(settings)
    if not math.isfinite(duration_sec) or not 0 < duration_sec <= 1800:
        raise ValueError("Live2D 视频时长须在 0～1800 秒之间")
    if not (64 <= width <= 1024 and 64 <= height <= 1024 and 1 <= fps <= 60):
        raise ValueError("Live2D 画布或帧率超出限制")
    if not math.isfinite(mouth_gain) or not 0 <= mouth_gain <= 3:
        raise ValueError("Live2D 口型强度超出限制")
    model = Path(model_path).resolve()
    if not model.is_file() or not model.name.endswith(".model3.json"):
        raise ValueError("Live2D 模型文件不存在")
    if not audio_path or not Path(audio_path).is_file():
        raise ValueError("Live2D 口型需要实际 TTS 旁白，当前任务没有可用旁白")
    root = Path(output_dir).resolve() / ("presenter-" + uuid.uuid4().hex[:12])
    root.mkdir(parents=True, exist_ok=False)
    config = {"libraries": libraries, "model": str(model), "audio": str(Path(audio_path).resolve()),
              "duration": duration_sec, "width": width, "height": height, "fps": fps,
              "mouth_parameter": mouth_parameter, "gain": mouth_gain or 1,
              "chrome": os.environ.get("SCID_CHROME", "")}
    config_path = root / "capture.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    timeout = min(settings.live2d_timeout_sec, timeout_sec if timeout_sec is not None else settings.live2d_timeout_sec)
    if timeout <= 0:
        raise RuntimeError("Live2D 渲染请求已超时")
    result = runner.run([sys.executable, str(Path(__file__).with_name("presenter_capture.py")), str(config_path)],
                        cwd=root, limits=ResourceLimits(timeout_sec=timeout))
    video = root / "presenter.webm"
    if not result.ok or not video.is_file():
        raise RuntimeError("Live2D 渲染失败：" + result.summary() + " " + result.tail(1500))
    return {"video_path": str(video), "envelope_path": str(root / "mouth.json"), "lip_sync": True}
