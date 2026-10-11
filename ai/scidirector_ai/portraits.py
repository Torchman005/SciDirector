"""Offline, data-only portrait packs. No synthetic replacement artwork."""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

STATES = {"neutral", "explain", "think", "surprised"}
POSE_FADE_SEC = .18
SPEECH_HOLD_SEC = .45


class Pose(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image: str
    mouth_open: str = ""


class Cue(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    time: float = Field(ge=0, le=1)
    state: str


class PortraitManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int
    states: dict[str, Pose]
    cues: list[Cue] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_contract(self):
        if self.version != 1 or set(self.states) != STATES:
            raise ValueError("立绘需要 version=1 及 neutral/explain/think/surprised 四种状态")
        mouths = sum(bool(p.mouth_open) for p in self.states.values())
        if mouths not in {0, 4}:
            raise ValueError("四种状态应全部提供口型图，或全部省略")
        if any(c.state not in STATES for c in self.cues) or any(b.time <= a.time for a,b in zip(self.cues,self.cues[1:])):
            raise ValueError("立绘状态时间表无效")
        return self


def load_portrait(path: Path) -> tuple[PortraitManifest, dict[str, Path]]:
    from PIL import Image

    manifest = PortraitManifest.model_validate_json(path.read_text(encoding="utf-8"))
    allowed: dict[str, Path] = {}
    size = None
    for pose in manifest.states.values():
        for name in (pose.image, pose.mouth_open):
            if not name:
                continue
            file = (path.parent / name).resolve()
            if not file.is_relative_to(path.parent.resolve()) or not file.is_file() or file.suffix.lower() != ".png":
                raise ValueError("立绘图片路径超出素材目录或不存在")
            with Image.open(file) as image:
                if image.format != "PNG" or max(image.size) > 4096 or min(image.size) < 1:
                    raise ValueError("立绘图片尺寸或格式无效")
                if size is not None and image.size != size:
                    raise ValueError("立绘图片画布尺寸不一致")
                size = image.size
                image.verify()
            allowed["/"+name] = file
    return manifest, allowed


def portrait_timeline(manifest: PortraitManifest, envelope: list[float], hz: int, duration: float) -> list[dict]:
    """Explicit expressive cues win; automatic speech poses avoid invented emotions."""
    cues = [{"time": 0., "state": "neutral"}]
    if manifest.cues:
        for cue in manifest.cues:
            item = {"time": cue.time * duration, "state": cue.state}
            if cue.time == 0:
                cues[0] = item
            else:
                cues.append(item)
        return cues
    last_speech = -SPEECH_HOLD_SEC * 2
    for index, value in enumerate(envelope):
        t = index / hz
        if value > .08:
            last_speech = t
        state = "explain" if t-last_speech < SPEECH_HOLD_SEC else "neutral"
        if state != cues[-1]["state"]:
            cues.append({"time":t,"state":state})
    return cues
