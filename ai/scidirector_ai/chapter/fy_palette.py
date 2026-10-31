"""Shared, explicit fy palette contract; never fill missing roles with paper defaults."""
from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, field_validator

HEX_COLOR = re.compile(r'^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$')


class FyPalette(BaseModel):
    model_config = ConfigDict(extra='forbid')

    paper: str
    card: str
    ink: str
    mute: str
    line: str
    q: str
    k: str
    v: str
    gold: str
    blue: str

    @field_validator('*', mode='before')
    @classmethod
    def _hex_color(cls, value: object) -> str:
        if not isinstance(value, str) or not HEX_COLOR.fullmatch(value.strip()):
            raise ValueError('必须为 #RGB、#RGBA、#RRGGBB 或 #RRGGBBAA 十六进制颜色')
        return value.strip()
