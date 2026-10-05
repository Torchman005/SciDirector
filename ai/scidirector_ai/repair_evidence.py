"""Bounded local prechecks. Visible change is evidence to review, never proof of repair."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image

from .media import FrameSample, MediaToolError, extract_frames_with_times
from .review_tasks import active_repairs
from .sandbox.runner import SandboxRunner
from .schemas import CriticFeedback, RenderArtifact, RepairTask

# Small local evidence images bound storage and decode cost; VLM retains full current frames.
EVIDENCE_WIDTH = 320


def repair_times(task: RepairTask, samples: list[dict[str, Any]]) -> list[float]:
    if task.frame_indices:
        if any(i > len(samples) for i in task.frame_indices):
            return []
        return sorted({float(samples[i - 1]["ts"]) for i in task.frame_indices})
    if task.end_sec > task.start_sec:
        margin = min(0.05, (task.end_sec - task.start_sec) / 4)
        return [task.start_sec + margin, (task.start_sec + task.end_sec) / 2,
                task.end_sec - margin]
    return []


def _pixels(path: str, region: list[float]) -> tuple[tuple[int, int], bytes]:
    with Image.open(path) as source:
        image = source.convert("RGB")
        image = image.resize((EVIDENCE_WIDTH, max(1, round(image.height * EVIDENCE_WIDTH / image.width))))
        if region:
            x, y, w, h = region
            left, top = int(x * image.width), int(y * image.height)
            right, bottom = round((x + w) * image.width), round((y + h) * image.height)
            if right <= left or bottom <= top:
                raise ValueError("repair region is smaller than one evidence pixel")
            image = image.crop((left, top, right, bottom))
        return image.size, image.tobytes()


def _samples_at(
    artifact: RenderArtifact, available: list[dict[str, Any]], times: list[float],
    out_dir: Path, runner: SandboxRunner,
) -> dict[float, str]:
    found = {float(s["ts"]): str(s["path"]) for s in available
             if float(s["ts"]) in times and Path(str(s["path"])).is_file()}
    missing = [t for t in times if t not in found and t < artifact.duration_sec]
    if missing:
        extra = extract_frames_with_times(artifact.video_path, out_dir, runner,
                                          duration_sec=artifact.duration_sec,
                                          width=EVIDENCE_WIDTH, timestamps=missing)
        found.update({s.ts: s.path for s in extra})
    return found


def precheck_repairs(
    feedback: CriticFeedback | None, previous: RenderArtifact | None, current: RenderArtifact,
    previous_samples: list[dict[str, Any]], current_samples: list[FrameSample],
    out_dir: Path, runner: SandboxRunner,
) -> list[dict[str, Any]]:
    tasks = active_repairs(feedback)
    if not tasks:
        return []
    rows = [{"task_id": task.task_id, "status": "unverified", "pairs": [],
             "reason": "缺少可对齐的旧证据或目标采样"} for task in tasks]
    # A legacy/reused video path cannot supply immutable before evidence.
    if previous is None or previous.video_path == current.video_path or not previous_samples:
        return rows
    target_times = {task.task_id: repair_times(task, previous_samples) for task in tasks}
    times = sorted({t for ts in target_times.values() for t in ts})
    if not times:
        return rows
    try:
        before = _samples_at(previous, previous_samples, times, out_dir / "before", runner)
        after = _samples_at(current, [{"path": s.path, "ts": s.ts} for s in current_samples],
                            times, out_dir / "after", runner)
    except (OSError, ValueError, MediaToolError):
        return rows
    for task, row in zip(tasks, rows):
        ts = target_times[task.task_id]
        if not ts or any(t not in before or t not in after for t in ts):
            continue
        row["pairs"] = [{"ts": t, "before": before[t], "after": after[t]} for t in ts]
        try:
            changed = any(_pixels(before[t], task.region) != _pixels(after[t], task.region) for t in ts)
        except (OSError, ValueError):
            row["pairs"] = []
            continue
        row["status"] = "changed" if changed else "unchanged"
        row["reason"] = "目标采样区域发生变化，需语义验收" if changed else "目标采样区域没有可见变化"
        # Sparse stills cannot rule out timing/transition changes or a moved target outside an old crop.
        row["can_skip"] = (not changed and task.category == "readability" and not task.region
                           and bool(task.frame_indices) and task.end_sec == task.start_sec)
    return rows


def skip_full_review(rows: list[dict[str, Any]]) -> bool:
    return bool(rows) and all(row.get("can_skip", False) for row in rows)
