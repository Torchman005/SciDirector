"""节奏检查（画面是否贯穿整段时长都在变化）。

守的是一条真实事故（`job-afdfcd4350073365-s000`）：
一条 **22.83 秒**的镜头，动画演到约 **30%** 处就基本静止 ——
相邻抽帧的"变化像素占比"一路是 `0.19% / 0.12% / 0.06%`。
审查连续四轮判它"动画停滞"（**判断是对的**），但每轮给的都是
"把打字 `run_time` 从 0.5 延长到 3 秒"这类**量级不匹配**的微调，
填不满那十几秒 —— 于是代码改来改去画面不变、审查结论一字不差，
一直烧到人工介入。

这个检查把"节奏不好"这种观感换成**算出来的具体时段**：
"第 13.3 秒到第 17.1 秒画面没有变化"。编码端才能照着改。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from scidirector_ai.graph.nodes import _collect_feedback
from scidirector_ai.media import (
    FrameSample,
    MotionReport,
    analyze_motion,
    frame_sample_times,
)

WHITE = (255, 255, 255)
BLACK = (0, 0, 0)


def _frame(path: Path, *, filled: bool | None = None, box: tuple[int, int] | None = None) -> str:
    """造一张纯白图（``filled`` 控制整幅是否填黑）或带一个黑方块。"""
    img = Image.new("L", (120, 90), color=255)
    draw = ImageDraw.Draw(img)
    if filled:
        draw.rectangle([0, 0, 120, 90], fill=0)
    elif box is not None:
        x, y = box
        draw.rectangle([x, y, x + 40, y + 30], fill=0)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return str(path)


class TestFrameSampleTimes:
    """采样时间点必须只有一份实现（抽帧与节奏检查共用）。"""

    def test_is_sorted_deduped_and_reaches_the_end(self) -> None:
        ts = frame_sample_times(22.83, 4)
        assert ts == sorted(ts)
        assert len(ts) == len(set(ts))
        assert ts[0] > 0.0, "首帧不能取 0.0（开场镜头在 t=0 是空帧）"
        assert ts[-1] == 22.83 - 0.05

    def test_includes_mid_samples_plus_first_and_last(self) -> None:
        ts = frame_sample_times(20.0, 4)
        # 4 个中点 + 首 + 末 = 6
        assert len(ts) == 6

    def test_degenerate_duration_does_not_explode(self) -> None:
        ts = frame_sample_times(0.0, 4)
        assert ts and ts == sorted(ts)


class TestAnalyzeMotion:
    """静止时段必须被**算出来**，并带上具体时间。"""

    def test_all_static_frames_are_flagged_with_times(self, tmp_path: Path) -> None:
        times = [0.2, 5.0, 10.0, 15.0, 20.0]
        samples = [
            FrameSample(path=_frame(tmp_path / f"s{i}.png"), ts=t)
            for i, t in enumerate(times)
        ]
        report = analyze_motion(samples)

        assert not report.ok
        # 最后一个间隔被忽略（提示词允许收尾停住），所以 4 个间隔里报 3 个。
        assert len(report.static_spans) == 3
        assert report.static_spans[0][:2] == (0.2, 5.0)
        assert report.static_spans[-1][:2] == (10.0, 15.0)

    def test_changing_frames_are_not_flagged(self, tmp_path: Path) -> None:
        times = [0.2, 5.0, 10.0, 15.0, 20.0]
        # 每帧的黑方块位置都不同 -> 每一段都有大量像素变化
        samples = [
            FrameSample(path=_frame(tmp_path / f"c{i}.png", box=(5 + i * 12, 10)), ts=t)
            for i, t in enumerate(times)
        ]
        report = analyze_motion(samples)
        assert report.ok
        assert report.summary() == "", "通过时不该产出反馈文本"

    def test_last_interval_is_ignored(self, tmp_path: Path) -> None:
        """收尾停住是允许的（提示词要求最终完成态在最后到位）。"""
        times = [0.2, 5.0, 10.0]
        samples = [
            FrameSample(path=_frame(tmp_path / "a.png", box=(0, 0)), ts=times[0]),
            FrameSample(path=_frame(tmp_path / "b.png", box=(60, 40)), ts=times[1]),
            FrameSample(path=_frame(tmp_path / "c.png", box=(60, 40)), ts=times[2]),
        ]
        report = analyze_motion(samples)
        assert report.ok, "只有最后一段静止 -> 不算缺陷"

    def test_very_short_interval_is_skipped(self, tmp_path: Path) -> None:
        """采样点挨得太近时画面变化本来就小，那是采样密度问题。"""
        samples = [
            FrameSample(path=_frame(tmp_path / "a.png"), ts=1.0),
            FrameSample(path=_frame(tmp_path / "b.png"), ts=1.2),
            FrameSample(path=_frame(tmp_path / "c.png", box=(10, 10)), ts=9.0),
            FrameSample(path=_frame(tmp_path / "d.png", box=(50, 40)), ts=17.0),
        ]
        report = analyze_motion(samples, min_span_sec=1.0)
        starts = [s[0] for s in report.static_spans]
        assert 1.0 not in starts, "间隔只有 0.2 秒，不该被判定"

    def test_summary_is_actionable_and_time_stamped(self, tmp_path: Path) -> None:
        """反馈必须带**具体时段** —— 这正是它比"节奏不好"有用的地方。"""
        times = [0.2, 5.0, 10.0, 15.0]
        samples = [
            FrameSample(path=_frame(tmp_path / f"x{i}.png"), ts=t)
            for i, t in enumerate(times)
        ]
        text = analyze_motion(samples).summary()
        assert "第 0.2 秒 → 第 5.0 秒" in text
        assert "变化像素占比" in text
        # 必须点明"延长已有动画填不满"，否则编码端仍会去微调 run_time。
        assert "填不满" in text or "拆成阶段" in text

    def test_threshold_is_respected(self, tmp_path: Path) -> None:
        """阈值是"**低于**它就判静止"，所以调大只会判得更严。

        两个方向都要测：默认 0.005 会把"完全没变化"（占比 0.0）判出来；
        设成 0.0 则意味着"零变化也不算缺陷"，此时不该有任何静止时段。
        """
        times = [0.2, 5.0, 10.0]
        samples = [
            FrameSample(path=_frame(tmp_path / f"t{i}.png"), ts=t)
            for i, t in enumerate(times)
        ]
        default = analyze_motion(samples, min_change_ratio=0.005)
        never_flag = analyze_motion(samples, min_change_ratio=0.0)
        always_flag = analyze_motion(samples, min_change_ratio=1.0)

        assert not default.ok, "完全没变化应当被判出来"
        assert never_flag.ok, "阈值 0.0 表示零变化也不算缺陷"
        assert not always_flag.ok, "阈值 1.0 表示任何变化都不够"


class TestFeedbackIncludesPacingMeasurement:
    """算出来的时段必须真的回灌给编码端。"""

    def _state(self, report: str) -> dict:
        return {"feedback": {}, "human_feedback": {}, "motion_reports": {"s000": report}}

    def test_included_when_present(self) -> None:
        text = _collect_feedback(self._state("【画面变化的量化测量】第 3 秒 → 第 8 秒没有变化"), "s000")
        assert "量化测量" in text
        assert "第 3 秒" in text

    def test_absent_when_no_report(self) -> None:
        text = _collect_feedback({"feedback": {}, "human_feedback": {}}, "s000")
        assert "量化测量" not in text

    def test_other_shots_report_does_not_leak(self) -> None:
        """按 shot_id 索引就是为了不把别的镜头的静止时段算到它头上。"""
        state = self._state("【画面变化的量化测量】这是 s000 的问题")
        text = _collect_feedback(state, "s001")
        assert "量化测量" not in text


class TestMotionReportShape:
    def test_ok_property_and_summary(self) -> None:
        assert MotionReport(ratios=[0.5], static_spans=[], min_change_ratio=0.005).ok
        assert not MotionReport(
            ratios=[0.001], static_spans=[(1.0, 2.0, 0.001)], min_change_ratio=0.005
        ).ok
