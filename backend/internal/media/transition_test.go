package media

import (
	"math"
	"strconv"
	"strings"
	"testing"
)

// TestPlanTransitionsOffsetsAccountForPreviousTransitions 是本文件最重要的一条。
//
// 它锁死 xfade 链式拼接最容易写错的地方：第 k 个连接点的 offset 必须基于
// 「前面所有片段拼接并扣掉已发生的转场之后」的游标，而不是简单地看着
// 前一个片段的时长去算。两种写法在第 1 个连接点上结果相同，从第 2 个开始分道扬镳 ——
// 这正是这类 bug 能通过一半测试、却在成片里表现为「后面的转场位置越来越偏」的原因。
func TestPlanTransitionsOffsetsAccountForPreviousTransitions(t *testing.T) {
	durations := []float64{4, 4, 4}
	plan := PlanTransitions(durations, TransitionSpec{Type: TransitionFade, DurationSec: 1})

	if !plan.Enabled {
		t.Fatalf("应当启用转场，实际未启用：%s", plan.Reason)
	}
	if len(plan.Offsets) != 2 {
		t.Fatalf("3 个片段应有 2 个连接点，实际 %d", len(plan.Offsets))
	}

	// L0 = 4；offset1 = L0 - T = 3；L1 = 4 + 4 - 1 = 7；offset2 = L1 - T = 6。
	wantOffsets := []float64{3, 6}
	for i, want := range wantOffsets {
		if math.Abs(plan.Offsets[i]-want) > 1e-9 {
			t.Errorf("offsets[%d] = %.3f，期望 %.3f", i, plan.Offsets[i], want)
		}
	}

	// 成片总时长 = 各片段之和 - (n-1)*T = 12 - 2 = 10。
	if math.Abs(plan.OutDuration-10) > 1e-9 {
		t.Errorf("OutDuration = %.3f，期望 10.000", plan.OutDuration)
	}

	// 反面对照：错误实现会算出 offset2 = d[1] - T = 3。
	// 明确断言它「不等于」错误值，让这条测试在有人改回错误写法时必然失败。
	if math.Abs(plan.Offsets[1]-3) < 1e-9 {
		t.Error("offsets[1] 等于错误实现的取值 3 —— offset 未累积扣减先前的转场")
	}
}

// TestPlanTransitionsOutDurationShrinksByTransitions 验证总时长确实变短了。
// 转场是「交叠」而不是「插入」，成片比简单相加更短 —— 这一点常被误解，
// 进而导致字幕轴按错误的总时长去对齐。
func TestPlanTransitionsOutDurationShrinksByTransitions(t *testing.T) {
	durations := []float64{5, 5, 5, 5}
	plan := PlanTransitions(durations, TransitionSpec{Type: TransitionFade, DurationSec: 0.5})
	if !plan.Enabled {
		t.Fatalf("应当启用：%s", plan.Reason)
	}
	// 20 - 3*0.5 = 18.5
	if math.Abs(plan.OutDuration-18.5) > 1e-9 {
		t.Fatalf("OutDuration = %.3f，期望 18.500", plan.OutDuration)
	}
}

// TestPlanTransitionsUsesUniformDurationClampedByShortestClip 验证转场时长
// 被最短片段压住，且**所有连接点取同一个值** —— 长短不一的转场观感廉价。
func TestPlanTransitionsUsesUniformDurationClampedByShortestClip(t *testing.T) {
	// 中间那个片段只有 0.8s，配置的 1.5s 必须被压到 0.8s。
	durations := []float64{4, 0.8, 4}
	plan := PlanTransitions(durations, TransitionSpec{Type: TransitionFade, DurationSec: 1.5})
	if !plan.Enabled {
		t.Fatalf("应当启用：%s", plan.Reason)
	}
	if math.Abs(plan.Duration-0.8) > 1e-9 {
		t.Fatalf("统一转场时长 = %.3f，期望被最短片段压到 0.800", plan.Duration)
	}
	// L0=4；offset1 = 4-0.8 = 3.2；L1 = 4+0.8-0.8 = 4；offset2 = 4-0.8 = 3.2
	if math.Abs(plan.Offsets[0]-3.2) > 1e-9 || math.Abs(plan.Offsets[1]-3.2) > 1e-9 {
		t.Fatalf("offsets = %v，期望 [3.2 3.2]", plan.Offsets)
	}
}

// TestPlanTransitionsDisabledCases 覆盖各种「应降级为硬切」的场景。
// 每个场景都必须给出**可读的原因** —— 静默降级会让「转场怎么没生效」
// 变成需要读源码才能回答的问题。
func TestPlanTransitionsDisabledCases(t *testing.T) {
	cases := []struct {
		name      string
		durations []float64
		spec      TransitionSpec
		wantIn    string
	}{
		{"空片段列表", nil, TransitionSpec{Type: TransitionFade, DurationSec: 1}, "没有可拼接"},
		{"单个片段", []float64{4}, TransitionSpec{Type: TransitionFade, DurationSec: 1}, "只有一个片段"},
		{"类型为 none", []float64{4, 4}, TransitionSpec{Type: TransitionNone, DurationSec: 1}, "硬切"},
		{"类型为空", []float64{4, 4}, TransitionSpec{DurationSec: 1}, "硬切"},
		{"时长为 0", []float64{4, 4}, TransitionSpec{Type: TransitionFade, DurationSec: 0}, "转场时长为 0"},
		{"时长为负", []float64{4, 4}, TransitionSpec{Type: TransitionFade, DurationSec: -1}, "转场时长为 0"},
		{"片段过短", []float64{0.05, 0.05}, TransitionSpec{Type: TransitionFade, DurationSec: 0.04}, "降级为硬切"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			plan := PlanTransitions(c.durations, c.spec)
			if plan.Enabled {
				t.Fatalf("该场景不应启用转场，实际启用了（duration=%.3f）", plan.Duration)
			}
			if !strings.Contains(plan.Reason, c.wantIn) {
				t.Fatalf("降级原因 %q 未包含 %q —— 原因必须可读", plan.Reason, c.wantIn)
			}
		})
	}
}

// TestPlanTransitionsNeverOverlapsBeyondClips 是安全性断言：
// offset 必须为正，且 offset + T 不能越过已拼接结果的末尾，
// 否则 xfade 会报错或产出黑帧。
func TestPlanTransitionsNeverOverlapsBeyondClips(t *testing.T) {
	// 用一批差异极大的时长做性质测试，而不是只测几个手挑的例子。
	suite := [][]float64{
		{1, 1}, {10, 1}, {1, 10}, {2, 3, 4}, {0.5, 5, 0.5}, {3, 3, 3, 3, 3},
	}
	for _, durations := range suite {
		for _, td := range []float64{0.1, 0.3, 0.5, 1.0, 2.0} {
			plan := PlanTransitions(durations, TransitionSpec{Type: TransitionFade, DurationSec: td})
			if !plan.Enabled {
				continue
			}

			// 复算游标，逐点校验 offset 的合法性。
			cursor := durations[0]
			for k := 1; k < len(durations); k++ {
				off := plan.Offsets[k-1]
				if off < 0 {
					t.Fatalf("d=%v T=%.2f：offset[%d] = %.3f 为负", durations, td, k-1, off)
				}
				// xfade 要求 offset + duration <= 前一段时长（此处即 cursor）。
				if off+plan.Duration > cursor+1e-9 {
					t.Fatalf("d=%v T=%.2f：offset[%d]+T = %.3f 越过前段末尾 %.3f",
						durations, td, k-1, off+plan.Duration, cursor)
				}
				if plan.Duration > durations[k]+1e-9 {
					t.Fatalf("d=%v：转场 %.3f 超过片段 %d 的时长 %.3f",
						durations, plan.Duration, k, durations[k])
				}
				cursor = cursor + durations[k] - plan.Duration
			}
			if math.Abs(cursor-plan.OutDuration) > 1e-9 {
				t.Fatalf("d=%v：复算总时长 %.3f 与计划 %.3f 不符", durations, cursor, plan.OutDuration)
			}
		}
	}
}

// TestBuildXFadeFilterStructure 校验滤镜图的连接关系。
func TestBuildXFadeFilterStructure(t *testing.T) {
	plan := PlanTransitions([]float64{4, 4, 4}, TransitionSpec{Type: TransitionFade, DurationSec: 1})
	fc, err := BuildXFadeFilter(plan, 3)
	if err != nil {
		t.Fatalf("构建滤镜图失败: %v", err)
	}

	// 每个输入都要有时基归零与格式化。
	for i := 0; i < 3; i++ {
		idx := strconv.Itoa(i)
		if !strings.Contains(fc, "["+idx+":v]setpts=PTS-STARTPTS") {
			t.Errorf("缺少输入 %d 的视频时基归零：%s", i, fc)
		}
		if !strings.Contains(fc, "["+idx+":a]asetpts=PTS-STARTPTS") {
			t.Errorf("缺少输入 %d 的音频时基归零：%s", i, fc)
		}
	}

	// 两条 xfade，offset 依次为 3 与 6。
	if n := strings.Count(fc, "xfade=transition=fade"); n != 2 {
		t.Errorf("应有两处 xfade，实际 %d 处：%s", n, fc)
	}
	if !strings.Contains(fc, "offset=3.000") || !strings.Contains(fc, "offset=6.000") {
		t.Errorf("offset 未按累积游标计算：%s", fc)
	}
	// 两条 acrossfade，时长与视频一致 —— 否则音画会逐段累积错位。
	if n := strings.Count(fc, "acrossfade=d=1.000"); n != 2 {
		t.Errorf("应有 2 处 acrossfade 且时长与视频一致，实际 %d：%s", n, fc)
	}
	if !strings.HasSuffix(fc, "[aout]") {
		t.Errorf("滤镜图应以 [aout] 结尾：%s", fc)
	}
	if !strings.Contains(fc, "[vout]") {
		t.Errorf("滤镜图缺少 [vout] 输出：%s", fc)
	}
}

// TestBuildXFadeFilterRejectsMismatchedPlan 守住参数一致性：
// offset 数量对不上时宁可报错，也不要生成一张静默错位的滤镜图。
func TestBuildXFadeFilterRejectsMismatchedPlan(t *testing.T) {
	plan := PlanTransitions([]float64{4, 4, 4}, TransitionSpec{Type: TransitionFade, DurationSec: 1})
	if _, err := BuildXFadeFilter(plan, 5); err == nil {
		t.Fatal("片段数与 offset 不匹配时应报错")
	}
	if _, err := BuildXFadeFilter(TransitionPlan{Enabled: false}, 3); err == nil {
		t.Fatal("未启用的方案不应能构建滤镜图")
	}
}

func TestParseTransitionType(t *testing.T) {
	for _, ok := range []string{"", "none", "fade", "FADE", " WipeLeft ", "dissolve"} {
		if _, err := ParseTransitionType(ok); err != nil {
			t.Errorf("ParseTransitionType(%q) 不应报错: %v", ok, err)
		}
	}
	// 拼错必须报错：静默降级为硬切会让配置错误永远不被发现。
	if _, err := ParseTransitionType("fadee"); err == nil {
		t.Error("未知转场名应报错")
	}
}
