"""Main-pipeline fy adapter: single-shot timing and model-code repair extraction."""
from __future__ import annotations

import json
import re

from .schemas import ShotSpec


def shot_timing(shot: ShotSpec) -> dict:
    # Main pipeline creates narration audio after code generation. These anchors
    # are proportional estimates, not provider word timestamps.
    chars = re.sub(r'''[\s，。！？、；：,.!?;:…—“”‘’"'（）()《》]+''', '', shot.narration).lower()
    return {'dur': shot.duration_sec, 'timing_basis': 'shot_estimate', 'lines': [
        {'id': 'shot', 'sub': shot.narration, 'start': 0, 'end': shot.duration_sec,
         'chars': chars, 'times': [shot.duration_sec * i / max(1, len(chars)) for i in range(len(chars))]}]}


def extract_fy_code(html: str) -> str:
    if not html:
        return ''
    match = re.search(r'<script id="scid-fy-code" type="application/json">(.*?)</script>', html, re.S)
    if not match:
        raise ValueError('上一版 fy 场景源码快照缺失，不能按旧 HTML 模板修复')
    code = json.loads(match[1])
    if not isinstance(code, str):
        raise ValueError('上一版 fy 场景源码无效')
    return code
