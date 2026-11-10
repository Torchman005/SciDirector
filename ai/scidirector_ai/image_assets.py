"""Embed only server-resolved, selected tenant assets into offline scenes."""
from __future__ import annotations

import base64
import re
from pathlib import Path

from .schemas import StyleGuide


def image_asset_data(style: StyleGuide, asset_ids) -> dict[str, str]:
    allowed = {asset.asset_id: asset for asset in style.scene_assets}
    result = {}
    for asset_id in sorted(set(asset_ids)):
        if asset_id not in allowed:
            raise ValueError(f'图片素材 {asset_id} 未被本任务选用')
        asset = allowed[asset_id]
        source = style.scene_asset_sources.get(asset_id)
        if not source:
            raise ValueError(f'图片素材 {asset_id} 缺少服务端解析路径')
        path = Path(source).resolve()
        suffix = '.png' if asset.mime_type == 'image/png' else '.jpg'
        if (path.stem != asset_id or path.suffix.lower() != suffix or
                path.parent.parent.name != 'visuals' or not re.fullmatch(r'[0-9a-f]{64}', path.parent.name)):
            raise ValueError(f'图片素材 {asset_id} 的路径无效')
        if not path.is_file() or path.stat().st_size != asset.size_bytes or asset.size_bytes > 2 << 20:
            raise ValueError(f'图片素材 {asset_id} 已丢失或内容被改动')
        result[asset_id] = f'data:{asset.mime_type};base64,{base64.b64encode(path.read_bytes()).decode("ascii")}'
    return result
