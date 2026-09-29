package media

import (
	"context"
	"path/filepath"
	"strings"
	"testing"
)

// ---------------------------------------------------------------------------
// 纯函数：何时需要对齐
// ---------------------------------------------------------------------------

func TestNeedsAlignUsesHalfFrameTolerance(t *testing.T) {
	// 容差取半帧：小于半帧的差异在帧率对齐时本来就会被抹平，
	// 为此多补一帧反而会引入可见的顿挫。
	base := NormalizeSpec{Width: 320, Height: 240, FPS: 30, AlignTo: 8}

	exact := base
	exact.AlignFrom = 8.0
	if exact.NeedsAlign() {
		t.Error("完全一致时不该需要对齐")
	}

	tiny := base
	tiny.AlignFrom = 8.0 + 0.5/30 - 0.001 // 刚好在半帧容差之内
	if tiny.NeedsAlign() {
		t.Error("半帧以内的差异不该触发重新编码")
	}

	late := base
	late.AlignFrom = 12.3 // 实测里那个超长的 manim 镜头
	if !late.NeedsAlign() {
		t.Error("超过半帧的差异必须触发对齐")
	}

	early := base
	early.AlignFrom = 5.0
	if !early.NeedsAlign() {
		t.Error("短于计划的片段也必须对齐（要补帧）")
	}

	off := base
	off.AlignTo = 0
	off.AlignFrom = 12.3
	if off.NeedsAlign() {
		t.Error("未指定对齐目标时不该做任何对齐（旧任务没有计划时长）")
	}
}

func TestAlignPadSecOnlyPadsWhenShort(t *testing.T) {
	s := NormalizeSpec{FPS: 30, AlignTo: 8}

	s.AlignFrom = 5.0
	if pad := s.alignPadSec(); pad <= 2.9 || pad > 3.0 {
		t.Errorf("5s 补到 8s 应补约 3s，实际 %.3f", pad)
	}

	s.AlignFrom = 12.3
	if pad := s.alignPadSec(); pad != 0 {
		t.Errorf("超长时不该补帧（应走裁剪），实际补 %.3f", pad)
	}

	s.AlignFrom = 8.0
	if pad := s.alignPadSec(); pad != 0 {
		t.Errorf("刚好等长时不该补帧，实际补 %.3f", pad)
	}

	// 略短于半帧：在容差内，不补。
	s.AlignFrom = 8.0 - 0.5/30 + 0.001
	if pad := s.alignPadSec(); pad != 0 {
		t.Errorf("半帧以内的不足不该补帧，实际补 %.3f", pad)
	}
}

func TestNormalizeVideoFilterAddsTpadOnlyWhenShort(t *testing.T) {
	short := NormalizeSpec{Width: 320, Height: 240, FPS: 30, AlignTo: 8, AlignFrom: 5}
	if vf := short.normalizeVideoFilter(); !strings.Contains(vf, "tpad=stop_mode=clone") {
		t.Errorf("偏短的片段应当补帧（冻结最后一帧），实际滤镜链：%s", vf)
	}

	long := NormalizeSpec{Width: 320, Height: 240, FPS: 30, AlignTo: 8, AlignFrom: 12.3}
	if vf := long.normalizeVideoFilter(); strings.Contains(vf, "tpad") {
		t.Errorf("偏长的片段应当裁剪而不是补帧，实际滤镜链：%s", vf)
	}
}

// ---------------------------------------------------------------------------
// 真实 ffmpeg：对齐真的生效
// ---------------------------------------------------------------------------

// TestNormalizeAlignsLongClipByTrimming 验证超长片段被裁到计划时长。
func TestNormalizeAlignsLongClipByTrimming(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}
	r := newTestRunner(t, 1)
	dir := t.TempDir()
	src := filepath.Join(dir, "long.mp4")
	out := filepath.Join(dir, "aligned.mp4")
	makeColorClip(t, r, src, "red", 5)

	spec := NormalizeSpec{Width: 320, Height: 240, FPS: 15, AlignTo: 2, AlignFrom: 5}
	if err := r.Normalize(context.Background(), src, out, spec); err != nil {
		t.Fatalf("Normalize 失败: %v", err)
	}
	got := probeAudioSec(t, out)
	if got < 1.8 || got > 2.2 {
		t.Errorf("超长片段应被裁到 ~2s，实际 %.2fs", got)
	}
}

// TestNormalizeAlignsShortClipByFreezing 验证不足的片段被补到计划时长，
// 且补的是**冻结的最后一帧**而不是黑帧。
//
// 补黑会让观众以为片子断了 —— 这是两种实现里较隐蔽的一种错误：
// 时长对了，内容却没了。因此必须用像素证明，不能只断言时长。
func TestNormalizeAlignsShortClipByFreezing(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}
	r := newTestRunner(t, 1)
	dir := t.TempDir()
	src := filepath.Join(dir, "short.mp4")
	out := filepath.Join(dir, "aligned.mp4")
	makeColorClip(t, r, src, "red", 2)

	spec := NormalizeSpec{Width: 320, Height: 240, FPS: 15, AlignTo: 4, AlignFrom: 2}
	if err := r.Normalize(context.Background(), src, out, spec); err != nil {
		t.Fatalf("Normalize 失败: %v", err)
	}

	got := probeAudioSec(t, out)
	if got < 3.7 || got > 4.3 {
		t.Errorf("不足的片段应被补到 ~4s，实际 %.2fs", got)
	}

	// 3.5s 处已经是补出来的部分：必须仍然是**真实内容**（冻结的末帧），而不是黑。
	//
	// 对照必须取**同一输出内部**的前后两帧，而不是拿原片来比：
	// Normalize 的滤镜链带 in_range=auto:out_range=limited，它本身就会压缩
	// 亮度范围，拿原片比会把"色彩范围转换"误读成"补了黑帧"。
	// 同一条链出来的两帧相比，范围转换的影响自然抵消。
	head := frameStatsAt(t, out, 1.0)
	tail := frameStatsAt(t, out, 3.5)
	if tail["YAVG"] < head["YAVG"]*0.9 {
		t.Errorf("补出来的是黑帧而不是冻结的末帧：内容帧 YAVG=%.1f，补齐段 YAVG=%.1f",
			head["YAVG"], tail["YAVG"])
	}
}

// TestNormalizeWithoutAlignLeavesDurationAlone 是对照组：
// 不设对齐目标时必须完全不动时长，否则这次改动会影响所有旧任务。
func TestNormalizeWithoutAlignLeavesDurationAlone(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}
	r := newTestRunner(t, 1)
	dir := t.TempDir()
	src := filepath.Join(dir, "short.mp4")
	out := filepath.Join(dir, "plain.mp4")
	makeColorClip(t, r, src, "red", 2)

	spec := NormalizeSpec{Width: 320, Height: 240, FPS: 15}
	if err := r.Normalize(context.Background(), src, out, spec); err != nil {
		t.Fatalf("Normalize 失败: %v", err)
	}
	if got := probeAudioSec(t, out); got < 1.8 || got > 2.2 {
		t.Errorf("未开启对齐时时长不该变，实际 %.2fs（期望 ~2s）", got)
	}
}
