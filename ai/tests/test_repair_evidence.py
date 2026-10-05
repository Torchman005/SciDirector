"""Prechecks preserve evidence without treating pixel change as acceptance."""
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from scidirector_ai.media import FrameSample
from scidirector_ai.repair_evidence import precheck_repairs, repair_times, skip_full_review
from scidirector_ai.schemas import CriticFeedback, RenderArtifact
from test_repair_tasks import task


def test_region_ignores_background_but_requires_semantic_review(tmp_path: Path) -> None:
    paths = [tmp_path / "before.png", tmp_path / "after.png"]
    for path, color in zip(paths, ["white", "blue"]):
        image = Image.new("RGB", (320, 180), color)
        ImageDraw.Draw(image).rectangle((0, 0, 159, 179), fill="black")
        image.save(path)
    feedback = CriticFeedback(passed=False, suggestions=["separate labels"], repair_tasks=[task(frame_indices=[1], region=[0, 0, .5, 1])])
    def run() -> list[dict[str, Any]]:
        return precheck_repairs(feedback, RenderArtifact(video_path="old", duration_sec=5),
                                RenderArtifact(video_path="new", duration_sec=5),
                                [{"path": str(paths[0]), "ts": 1}], [FrameSample(str(paths[1]), 1)],
                                tmp_path, object())
    assert run()[0]["status"] == "unchanged"
    assert not skip_full_review(run())  # The target may have moved outside its original crop.
    feedback.repair_tasks[0].region = []
    assert run()[0]["status"] == "changed"


def test_unchanged_readability_skips_but_pacing_does_not(tmp_path: Path) -> None:
    path = tmp_path / "frame.png"
    Image.new("RGB", (320, 180), "black").save(path)
    feedback = CriticFeedback(passed=False, suggestions=["separate labels"], repair_tasks=[task(frame_indices=[1])])
    def run() -> list[dict[str, Any]]:
        return precheck_repairs(feedback, RenderArtifact(video_path="old", duration_sec=5),
                                RenderArtifact(video_path="new", duration_sec=5),
                                [{"path": str(path), "ts": 1}], [FrameSample(str(path), 1)],
                                tmp_path, object())
    assert skip_full_review(run())
    feedback.repair_tasks[0].category = "pacing"
    assert not skip_full_review(run())


def test_missing_old_evidence_is_unverified(tmp_path: Path) -> None:
    feedback = CriticFeedback(passed=False, suggestions=["separate labels"], repair_tasks=[task()])
    rows = precheck_repairs(feedback, None, RenderArtifact(), [], [], tmp_path, object())
    assert rows[0]["status"] == "unverified" and not skip_full_review(rows)
    assert repair_times(task(frame_indices=[9]), [{"ts": 1}]) == []
    times = repair_times(task(frame_indices=[], start_sec=1, end_sec=2), [])
    assert len(times) == 3 and all(1 <= t <= 2 for t in times)
