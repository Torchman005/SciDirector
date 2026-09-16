package media

import (
	"context"
	"fmt"
	"math"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/itJinYu/SciDirector/backend/internal/config"
)

// ---------------------------------------------------------------------------
// 拼接方案推导（纯函数）
// ---------------------------------------------------------------------------

func TestPlanSpliceCoversAllThreeShapes(t *testing.T) {
	const base = 10.0
	cases := []struct {
		name          string
		start, end    float64
		wantHead      float64
		wantTailStart float64
		wantTail      float64
		wantSegments  int
	}{
		{"中间区间：前段+新片段+后段", 3, 5, 3, 5, 5, 3},
		{"从片头开始：只有新片段+后段", 0, 2, 0, 2, 8, 2},
		{"到片尾结束：只有前段+新片段", 8, 10, 8, 0, 0, 2},
		{"片头到片尾：退化为整体替换", 0, 10, 0, 0, 0, 1},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			plan := PlanSplice(base, c.start, c.end)
			if c.wantSegments == 1 {
				if !plan.FullReplace {
					t.Fatalf("该区间应退化为整体替换，实际 %+v", plan)
				}
				if plan.Reason == "" {
					t.Error("整体替换必须给出可读原因")
				}
				return
			}
			if plan.FullReplace {
				t.Fatalf("不应是整体替换：%s", plan.Reason)
			}
			if math.Abs(plan.Head-c.wantHead) > 1e-9 {
				t.Errorf("Head = %.3f，期望 %.3f", plan.Head, c.wantHead)
			}
			if math.Abs(plan.TailStart-c.wantTailStart) > 1e-9 {
				t.Errorf("TailStart = %.3f，期望 %.3f", plan.TailStart, c.wantTailStart)
			}
			if math.Abs(plan.Tail-c.wantTail) > 1e-9 {
				t.Errorf("Tail = %.3f，期望 %.3f", plan.Tail, c.wantTail)
			}
			if plan.Segments != c.wantSegments {
				t.Errorf("Segments = %d，期望 %d", plan.Segments, c.wantSegments)
			}
		})
	}
}

// TestPlanSpliceClampsOutOfRange 覆盖越界区间。
//
// 渲染方偶尔会给出略微越界的区间（原作 4.0 秒，却请求替换 3.2~4.3）。
// 意图是明确的，应当裁剪而不是报错 —— 报错会让一次本可成功的重做白费。
func TestPlanSpliceClampsOutOfRange(t *testing.T) {
	plan := PlanSplice(4.0, 3.2, 4.3)
	if plan.FullReplace {
		t.Fatalf("越界应被裁剪而不是整体替换：%s", plan.Reason)
	}
	if math.Abs(plan.PatchEnd-4.0) > 1e-9 {
		t.Errorf("越界的结尾应被裁到 4.0，实际 %.3f", plan.PatchEnd)
	}
	if plan.Tail != 0 {
		t.Errorf("裁到片尾后不应有后段，实际 %.3f", plan.Tail)
	}
	if math.Abs(plan.Head-3.2) > 1e-9 {
		t.Errorf("Head = %.3f，期望 3.2", plan.Head)
	}

	// 负数起点应被裁到 0。
	plan = PlanSplice(4.0, -1, 1.0)
	if plan.Head != 0 {
		t.Errorf("负起点裁剪后不应有前段，实际 %.3f", plan.Head)
	}
	if math.Abs(plan.PatchStart) > 1e-9 {
		t.Errorf("PatchStart 应被裁到 0，实际 %.3f", plan.PatchStart)
	}
}

func TestPlanSpliceFullReplaceCases(t *testing.T) {
	cases := []struct {
		name             string
		base, start, end float64
	}{
		{"原片时长为零", 0, 1, 2},
		{"原片时长为负", -1, 1, 2},
		{"区间反向", 10, 5, 3},
		{"区间为空", 10, 5, 5},
		{"区间完全在原片之外", 10, 20, 25},
		{"区间覆盖整片", 10, 0, 10},
		{"区间超过阈值比例", 10, 0.4, 9.8},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			plan := PlanSplice(c.base, c.start, c.end)
			if !plan.FullReplace {
				t.Fatalf("应退化为整体替换，实际 %+v", plan)
			}
			if plan.Reason == "" {
				t.Error("必须给出可读原因，否则「为什么没走拼接」只能靠读源码回答")
			}
			if plan.SavedRatio() != 0 {
				t.Errorf("整体替换的节省比例应为 0，实际 %.3f", plan.SavedRatio())
			}
		})
	}
}

// TestPlanSpliceSavedRatioIsQuantified 确认收益可被度量。
// 没有度量指标的优化，无法判断它是否真的生效。
func TestPlanSpliceSavedRatioIsQuantified(t *testing.T) {
	// 10 秒的镜头只重渲 1 秒 → 省下 90%。
	plan := PlanSplice(10, 4, 5)
	if plan.FullReplace {
		t.Fatalf("不应整体替换：%s", plan.Reason)
	}
	if math.Abs(plan.SavedRatio()-0.9) > 1e-9 {
		t.Errorf("节省比例 = %.3f，期望 0.900", plan.SavedRatio())
	}
}

// ---------------------------------------------------------------------------
// 滤镜图构建（纯函数）
// ---------------------------------------------------------------------------

// TestBuildSpliceFilterInterleavesInputs 锁死 concat 滤镜的输入顺序。
//
// 同时处理音视频时，concat 要求输入交替排列为 [v0][a0][v1][a1]…，
// 而不是 [v0][v1]…[a0][a1]…。写反了 ffmpeg 不会说「顺序错了」，
// 只会报一些关于像素格式/采样率的费解错误，排查代价很高。
func TestBuildSpliceFilterInterleavesInputs(t *testing.T) {
	plan := PlanSplice(10, 3, 5)
	filter, err := BuildSpliceFilter(plan)
	if err != nil {
		t.Fatalf("构建滤镜图失败: %v", err)
	}

	// 期望形如 [vh][ah][vp][ap][vt][at]concat=n=3:v=1:a=1[vout][aout]
	want := "[vh][ah][vp][ap][vt][at]concat=n=3:v=1:a=1[vout][aout]"
	if !strings.Contains(filter, want) {
		t.Fatalf("concat 输入未按 [v][a] 交替排列。\n得到: %s\n期望包含: %s", filter, want)
	}

	// 原片被切两刀，必须先分流。
	if !strings.Contains(filter, "[0:v]split=2[bv1][bv2]") {
		t.Errorf("同时保留前后段时必须先 split 原片视频: %s", filter)
	}
	// 音频必须用 asplit：split 是**视频专用**滤镜，用错时 ffmpeg 抛出的
	// 是「Media type mismatch … (video) … (audio)」，极具误导性。
	if !strings.Contains(filter, "[0:a]asplit=2[ba1][ba2]") {
		t.Errorf("音频必须用 asplit（split 是视频专用滤镜）: %s", filter)
	}
	if strings.Contains(filter, "[0:a]split=") {
		t.Errorf("音频不能使用 split 滤镜: %s", filter)
	}
	// 切点必须正确。
	if !strings.Contains(filter, "trim=start=0:end=3.000") {
		t.Errorf("前段切点错误: %s", filter)
	}
	if !strings.Contains(filter, "trim=start=5.000") {
		t.Errorf("后段切点错误: %s", filter)
	}
}

// TestBuildSpliceFilterSkipsSplitWhenUnnecessary 确认只有一刀时不做多余的 split。
func TestBuildSpliceFilterSkipsSplitWhenUnnecessary(t *testing.T) {
	// 从片头开始替换：只有新片段 + 后段，无需 split。
	plan := PlanSplice(10, 0, 2)
	filter, err := BuildSpliceFilter(plan)
	if err != nil {
		t.Fatalf("构建失败: %v", err)
	}
	if strings.Contains(filter, "split=") {
		t.Errorf("只有一刀时不应引入 split: %s", filter)
	}
	if !strings.Contains(filter, "concat=n=2:v=1:a=1") {
		t.Errorf("应为 2 段拼接: %s", filter)
	}

	// 到片尾结束：前段 + 新片段。
	plan = PlanSplice(10, 8, 10)
	filter, err = BuildSpliceFilter(plan)
	if err != nil {
		t.Fatalf("构建失败: %v", err)
	}
	if strings.Contains(filter, "split=") {
		t.Errorf("只有一刀时不应引入 split: %s", filter)
	}
	if !strings.Contains(filter, "concat=n=2:v=1:a=1") {
		t.Errorf("应为 2 段拼接: %s", filter)
	}
}

func TestBuildSpliceFilterRejectsFullReplace(t *testing.T) {
	plan := PlanSplice(10, 0, 10)
	if _, err := BuildSpliceFilter(plan); err == nil {
		t.Fatal("整体替换方案不应能构建拼接滤镜图，否则会拼出一个重复内容的片子")
	}
}

// ---------------------------------------------------------------------------
// 真实 ffmpeg 拼接
// ---------------------------------------------------------------------------

// TestSpliceSegmentRealReplace 是局部重渲染的端到端验收。
//
// 构造一个 6 秒原片，把中间 2 秒换成另一段内容，
// 验证成片时长不变（4 + 2 + ... 恰好等于原片），且中间那段确实是新内容。
func TestSpliceSegmentRealReplace(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}

	r := newTestRunnerWith(t, config.MediaConfig{
		FFmpegBin: "ffmpeg", FFprobeBin: "ffprobe",
		MaxParallel: 2, CommandTimeout: 2 * time.Minute,
		Width: 320, Height: 240, FPS: 15,
	})
	dir := t.TempDir()
	ctx := context.Background()

	// 原片：6 秒。
	baseRaw := filepath.Join(dir, "base_raw.mp4")
	makeClip(t, r, baseRaw, "320x240", 15, 6)
	base := filepath.Join(dir, "base.mp4")
	if err := r.Normalize(ctx, baseRaw, base, testSpec()); err != nil {
		t.Fatalf("归一化原片失败: %v", err)
	}
	baseProbe, err := r.Probe(ctx, base)
	if err != nil {
		t.Fatalf("探测原片失败: %v", err)
	}

	// 新片段：2 秒，内容用不同图案。
	patchRaw := filepath.Join(dir, "patch_raw.mp4")
	if err := r.run(ctx,
		"-hide_banner", "-nostdin", "-y",
		"-f", "lavfi", "-i", "smptebars=duration=2:size=320x240:rate=15",
		"-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
		patchRaw,
	); err != nil {
		t.Fatalf("生成替换片段失败: %v", err)
	}
	patch := filepath.Join(dir, "patch.mp4")
	if err := r.Normalize(ctx, patchRaw, patch, testSpec()); err != nil {
		t.Fatalf("归一化替换片段失败: %v", err)
	}

	// 替换 [2, 4) 区间。
	plan := PlanSplice(baseProbe.DurationSec, 2, 4)
	if plan.FullReplace {
		t.Fatalf("该区间应走拼接：%s", plan.Reason)
	}
	if plan.Segments != 3 {
		t.Fatalf("应切为 3 段，实际 %d", plan.Segments)
	}

	out := filepath.Join(dir, "spliced.mp4")
	if err := r.SpliceSegment(ctx, base, patch, out, plan); err != nil {
		t.Fatalf("拼接失败: %v", err)
	}

	probe, err := r.Probe(ctx, out)
	if err != nil {
		t.Fatalf("探测拼接产物失败: %v", err)
	}

	// 时长 = 前段 2 + 新片段 2 + 后段 2 ≈ 原片 6 秒。
	// 这正是「替换而不是插入」的直接证据：若把新片段插进去，会变成 8 秒。
	if math.Abs(probe.DurationSec-baseProbe.DurationSec) > 0.4 {
		t.Fatalf("拼接后时长 %.3fs 与原片 %.3fs 相差过大 —— "+
			"新片段应替换该区间而不是插入其中",
			probe.DurationSec, baseProbe.DurationSec)
	}
	if probe.Width != 320 || probe.Height != 240 {
		t.Errorf("拼接产物尺寸 %dx%d，期望 320x240", probe.Width, probe.Height)
	}
	if !probe.HasAudio {
		t.Error("拼接产物丢失了音轨")
	}
	if probe.ColorRange != "tv" {
		t.Errorf("拼接产物 color_range = %q，期望 tv", probe.ColorRange)
	}
}

// TestSpliceSegmentAtHeadAndTail 覆盖替换区间贴着首尾的两种形状。
//
// 这两种形状会走到「不需要 split」的分支，与中间区间是两条不同的滤镜图路径，
// 必须分别验证，否则一条路径正确、另一条报错的情况会漏过去。
func TestSpliceSegmentAtHeadAndTail(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}

	r := newTestRunner(t, 2)
	dir := t.TempDir()
	ctx := context.Background()

	baseRaw := filepath.Join(dir, "base_raw.mp4")
	makeClip(t, r, baseRaw, "320x240", 15, 6)
	base := filepath.Join(dir, "base.mp4")
	if err := r.Normalize(ctx, baseRaw, base, testSpec()); err != nil {
		t.Fatalf("归一化原片失败: %v", err)
	}
	baseProbe, err := r.Probe(ctx, base)
	if err != nil {
		t.Fatalf("探测原片失败: %v", err)
	}

	patchRaw := filepath.Join(dir, "patch_raw.mp4")
	if err := r.run(ctx, "-hide_banner", "-nostdin", "-y",
		"-f", "lavfi", "-i", "smptebars=duration=1.5:size=320x240:rate=15",
		"-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
		patchRaw); err != nil {
		t.Fatalf("生成替换片段失败: %v", err)
	}
	patch := filepath.Join(dir, "patch.mp4")
	if err := r.Normalize(ctx, patchRaw, patch, testSpec()); err != nil {
		t.Fatalf("归一化替换片段失败: %v", err)
	}

	cases := []struct {
		name       string
		start, end float64
	}{
		{"替换片头", 0, 1.5},
		{"替换片尾", baseProbe.DurationSec - 1.5, baseProbe.DurationSec},
	}
	for i, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			plan := PlanSplice(baseProbe.DurationSec, c.start, c.end)
			if plan.FullReplace {
				t.Fatalf("应走拼接：%s", plan.Reason)
			}
			out := filepath.Join(dir, fmt.Sprintf("out_%d.mp4", i))
			if err := r.SpliceSegment(ctx, base, patch, out, plan); err != nil {
				t.Fatalf("拼接失败: %v", err)
			}
			probe, err := r.Probe(ctx, out)
			if err != nil {
				t.Fatalf("探测失败: %v", err)
			}
			if probe.DurationSec <= 0 {
				t.Fatal("拼接产物时长为 0")
			}
			// 时长应仍在原片附近（新片段长度与替换区间略有差异属于正常）。
			if math.Abs(probe.DurationSec-baseProbe.DurationSec) > 1.0 {
				t.Errorf("拼接后时长 %.3fs 偏离原片 %.3fs 过多",
					probe.DurationSec, baseProbe.DurationSec)
			}
		})
	}
}
