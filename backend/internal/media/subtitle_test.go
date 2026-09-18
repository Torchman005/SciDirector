package media

import (
	"math"
	"os"
	"strings"
	"testing"
)

// readFile 是 os.ReadFile 的薄包装，避免测试里到处引入 os。
func readFile(path string) ([]byte, error) { return os.ReadFile(path) }

// ---------------------------------------------------------------------------
// 镜头可见窗口
// ---------------------------------------------------------------------------

// TestPlanShotWindowsWithoutTransitionsIsCumulative 覆盖硬切路径：
// 窗口就是朴素的时长累加。
func TestPlanShotWindowsWithoutTransitionsIsCumulative(t *testing.T) {
	durations := []float64{2, 3, 4}
	windows := PlanShotWindows(durations, TransitionPlan{Enabled: false})

	want := []Window{{0, 2}, {2, 5}, {5, 9}}
	if len(windows) != len(want) {
		t.Fatalf("窗口数 = %d，期望 %d", len(windows), len(want))
	}
	for i := range want {
		if math.Abs(windows[i].Start-want[i].Start) > 1e-9 ||
			math.Abs(windows[i].End-want[i].End) > 1e-9 {
			t.Errorf("windows[%d] = [%.3f,%.3f]，期望 [%.3f,%.3f]",
				i, windows[i].Start, windows[i].End, want[i].Start, want[i].End)
		}
	}
}

// TestPlanShotWindowsAccountsForTransitionOverlap 是字幕对齐的核心断言。
//
// 启用转场后成片比片段之和短 (n-1)×T。若字幕仍按原始时长累加定位，
// 每过一个转场就偏移 T 秒，越往后错得越离谱。
func TestPlanShotWindowsAccountsForTransitionOverlap(t *testing.T) {
	durations := []float64{4, 4, 4}
	plan := PlanTransitions(durations, TransitionSpec{Type: TransitionFade, DurationSec: 1})
	if !plan.Enabled {
		t.Fatalf("应当启用转场：%s", plan.Reason)
	}

	windows := PlanShotWindows(durations, plan)

	// 朴素累加会得到 [0,4) [4,8) [8,12)，总长 12。
	// 正确结果：offset = [3, 6]，窗口 = [0,3) [3,6) [6,10)，总长 10。
	want := []Window{{0, 3}, {3, 6}, {6, 10}}
	for i := range want {
		if math.Abs(windows[i].Start-want[i].Start) > 1e-9 ||
			math.Abs(windows[i].End-want[i].End) > 1e-9 {
			t.Errorf("windows[%d] = [%.3f,%.3f]，期望 [%.3f,%.3f]",
				i, windows[i].Start, windows[i].End, want[i].Start, want[i].End)
		}
	}

	// 明确断言「没有退化成朴素累加」—— 这是本条测试真正的价值。
	if math.Abs(windows[2].End-12) < 1e-9 {
		t.Fatal("窗口末尾等于片段之和，说明转场的时长压缩没有被计入")
	}
}

// TestPlanShotWindowsTileTheTimeline 用性质测试覆盖多组输入：
// 窗口必须首尾相接、互不重叠、完整覆盖 [0, 成片总时长]。
//
// 这三条同时成立，才谈得上「字幕不会漂移」。
func TestPlanShotWindowsTileTheTimeline(t *testing.T) {
	suite := [][]float64{
		{2, 3}, {4, 4, 4}, {5, 1, 5}, {0.5, 5, 0.5}, {3, 3, 3, 3},
	}
	for _, durations := range suite {
		for _, td := range []float64{0, 0.3, 0.5, 1.0} {
			var plan TransitionPlan
			if td > 0 {
				plan = PlanTransitions(durations, TransitionSpec{Type: TransitionFade, DurationSec: td})
			}
			windows := PlanShotWindows(durations, plan)

			if len(windows) != len(durations) {
				t.Fatalf("d=%v td=%.2f：窗口数 %d 与片段数 %d 不符",
					durations, td, len(windows), len(durations))
			}
			if windows[0].Start != 0 {
				t.Fatalf("d=%v td=%.2f：首个窗口未从 0 开始（%.3f）", durations, td, windows[0].Start)
			}
			for i, w := range windows {
				if w.Duration() <= 0 {
					t.Fatalf("d=%v td=%.2f：窗口 %d 长度非正（%.3f）"+
						" —— 该镜头不会有任何字幕", durations, td, i, w.Duration())
				}
				if i > 0 {
					// 首尾相接：不允许有缝，也不允许重叠。
					if math.Abs(w.Start-windows[i-1].End) > 1e-9 {
						t.Fatalf("d=%v td=%.2f：窗口 %d 与 %d 不相接（%.3f vs %.3f）",
							durations, td, i-1, i, windows[i-1].End, w.Start)
					}
				}
			}

			wantTotal := 0.0
			for _, d := range durations {
				wantTotal += d
			}
			if plan.Enabled {
				wantTotal = plan.OutDuration
			}
			if math.Abs(windows[len(windows)-1].End-wantTotal) > 1e-9 {
				t.Fatalf("d=%v td=%.2f：窗口末尾 %.3f，期望成片时长 %.3f",
					durations, td, windows[len(windows)-1].End, wantTotal)
			}
		}
	}
}

// ---------------------------------------------------------------------------
// 断句
// ---------------------------------------------------------------------------

func TestSplitSentences(t *testing.T) {
	cases := []struct {
		in   string
		want int
	}{
		{"这是第一句。这是第二句。", 2},
		{"问句？陈述句。感叹！", 3},
		{"没有标点的一句话", 1},
		{"First sentence. Second sentence.", 2},
		{"分号；也是边界", 2},
		{"换行\n也算边界", 2},
		{"", 0},
	}
	for _, c := range cases {
		if got := len(splitSentences(c.in)); got != c.want {
			t.Errorf("splitSentences(%q) 得到 %d 句，期望 %d：%v",
				c.in, got, c.want, splitSentences(c.in))
		}
	}
}

// TestSplitSentencesDoesNotBreakDecimal 覆盖英文句点兼作小数点的场景。
// 「准确率 3.14」被切成「准确率 3」和「14」是没有意义的碎片。
func TestSplitSentencesDoesNotBreakDecimal(t *testing.T) {
	got := splitSentences("圆周率约等于 3.14 这个值")
	if len(got) != 1 {
		t.Fatalf("小数点不应断句，实际切成 %d 段：%v", len(got), got)
	}

	// 句末的句点仍要断。
	got = splitSentences("圆周率约等于 3.14. 下一句在这里")
	if len(got) != 2 {
		t.Fatalf("句末句点应断句，实际 %d 段：%v", len(got), got)
	}
}

func TestSplitLongSegmentHardSplits(t *testing.T) {
	// 30 个汉字、上限 12：必须被拆成多条。
	long := strings.Repeat("科", 30)
	parts := splitLongSegment(long, 12)
	if len(parts) < 2 {
		t.Fatalf("超长句应被拆分，实际 %d 段", len(parts))
	}
	for i, p := range parts {
		if speechWeight(p) > 12.5 {
			t.Errorf("第 %d 段权重 %.1f 超过上限 12：%q", i, speechWeight(p), p)
		}
	}
	// 内容不能丢：拼回去应等于原文。
	if strings.Join(parts, "") != long {
		t.Errorf("拆分后内容丢失：%v", parts)
	}

	// 短句不应被拆。
	if got := splitLongSegment("短句", 12); len(got) != 1 {
		t.Errorf("短句不应被拆分，实际 %v", got)
	}
}

func TestSpeechWeight(t *testing.T) {
	// 纯中文：一个字一个权重。
	if got := speechWeight("科学"); math.Abs(got-2) > 1e-9 {
		t.Errorf("speechWeight(科学) = %.2f，期望 2", got)
	}
	// 英文单词按 1.6 计：12 个字母的一个词只应占约 1.6，而不是 12。
	w := speechWeight("transformation")
	if w < 1.5 || w > 1.7 {
		t.Errorf("speechWeight(transformation) = %.2f，期望约 1.6（英文不应按字母数计）", w)
	}
	// 中英混排应按各自规则相加。
	mixed := speechWeight("傅里叶 Fourier")
	if math.Abs(mixed-(3+1.6)) > 1e-9 {
		t.Errorf("speechWeight(傅里叶 Fourier) = %.2f，期望 4.60", mixed)
	}
	// 空串与纯标点要有下限，避免参与比例分配时除零。
	if got := speechWeight("   "); got < 1 {
		t.Errorf("空串权重应有下限 1，实际 %.2f", got)
	}
}

// ---------------------------------------------------------------------------
// 字幕条生成
// ---------------------------------------------------------------------------

// TestPlanCuesStayInsideTheirWindow 是最重要的不变式：
// 每条字幕都必须落在所属镜头的可见窗口内，且互不重叠、时间单调递增。
func TestPlanCuesStayInsideTheirWindow(t *testing.T) {
	windows := []Window{{0, 3}, {3, 7}, {7, 10}}
	narrations := []string{
		"第一句。第二句。",
		"这是第三个镜头的第一句话，稍微长一点。然后是第二句。",
		"总结。",
	}

	cues := PlanCues(windows, narrations, DefaultSubtitleOptions())
	if len(cues) == 0 {
		t.Fatal("应当生成字幕")
	}

	prevEnd := -1.0
	for i, c := range cues {
		if c.End <= c.Start {
			t.Fatalf("cue %d 时长非正：[%.3f,%.3f]", i, c.Start, c.End)
		}
		if c.Start < prevEnd-1e-9 {
			t.Fatalf("cue %d 与前一条重叠：起点 %.3f < 上一条终点 %.3f", i, c.Start, prevEnd)
		}
		prevEnd = c.End
		if strings.TrimSpace(c.Text) == "" {
			t.Fatalf("cue %d 文本为空", i)
		}
	}

	// 每条字幕必须归属于某个窗口，不能越界。
	for i, c := range cues {
		inside := false
		for _, w := range windows {
			if c.Start >= w.Start-1e-9 && c.End <= w.End+1e-9 {
				inside = true
				break
			}
		}
		if !inside {
			t.Fatalf("cue %d [%.3f,%.3f] 落在所有镜头窗口之外 —— 字幕会与画面脱节", i, c.Start, c.End)
		}
	}
}

// TestPlanCuesAllocatesProportionallyToTextLength 验证「长句多分时间」。
func TestPlanCuesAllocatesProportionallyToTextLength(t *testing.T) {
	windows := []Window{{0, 10}}
	// 第一句 2 字，第二句 8 字，上半句的时间应约为下半句的 1/4。
	narrations := []string{"短句。这是一个明显更长的句子呀。"}

	cues := PlanCues(windows, narrations, SubtitleOptions{MaxCharsPerCue: 50, MinCueSec: 0.1, MaxCueSec: 100})
	if len(cues) != 2 {
		t.Fatalf("应生成 2 条字幕，实际 %d 条：%+v", len(cues), cues)
	}
	d0 := cues[0].End - cues[0].Start
	d1 := cues[1].End - cues[1].Start
	if !(d0 < d1) {
		t.Fatalf("短句分到的时间（%.2f）不应多于长句（%.2f）", d0, d1)
	}
	// 窗口末尾必须被完全用掉。
	if math.Abs(cues[1].End-10) > 1e-9 {
		t.Fatalf("最后一条字幕未对齐到窗口末尾：%.3f", cues[1].End)
	}
}

// TestPlanCuesMergesWhenWindowIsTight 覆盖「窗口太短」的边界。
//
// 2 秒窗口 + 最短显示 0.8 秒，最多只能放 2 条。若坚持拆成 5 条，
// 每条只有 0.4 秒，观众根本来不及读 —— 那等于没有字幕。
func TestPlanCuesMergesWhenWindowIsTight(t *testing.T) {
	windows := []Window{{0, 2}}
	narrations := []string{"一。二。三。四。五。"}

	opt := DefaultSubtitleOptions() // MinCueSec = 0.8
	cues := PlanCues(windows, narrations, opt)

	if len(cues) > 2 {
		t.Fatalf("2 秒窗口最多容纳 2 条（最短 %.1fs），实际 %d 条", opt.MinCueSec, len(cues))
	}
	for i, c := range cues {
		if c.End-c.Start < opt.MinCueSec-1e-9 {
			t.Fatalf("cue %d 仅 %.3fs，低于最短显示时长 %.1fs", i, c.End-c.Start, opt.MinCueSec)
		}
	}
	// 合并的是条数，不是内容：文字不能丢。
	joined := ""
	for _, c := range cues {
		joined += c.Text
	}
	for _, ch := range []string{"一", "二", "三", "四", "五"} {
		if !strings.Contains(joined, ch) {
			t.Errorf("合并后丢失了内容 %q：%q", ch, joined)
		}
	}
}

// TestPlanCuesSkipsEmptyNarration 覆盖空画外音：
// 不应生成空字幕条（空字幕在播放器里表现为一条莫名其妙消失的字幕）。
func TestPlanCuesSkipsEmptyNarration(t *testing.T) {
	windows := []Window{{0, 3}, {3, 6}}
	narrations := []string{"", "   "}
	if cues := PlanCues(windows, narrations, DefaultSubtitleOptions()); len(cues) != 0 {
		t.Fatalf("空画外音不应产生字幕，实际 %+v", cues)
	}
}

// TestPlanCuesHandlesFewerNarrationsThanWindows 覆盖数据不完整的情况：
// 画外音比镜头少时不应 panic，也不能给后面的镜头硬塞内容。
func TestPlanCuesHandlesFewerNarrationsThanWindows(t *testing.T) {
	windows := []Window{{0, 3}, {3, 6}, {6, 9}}
	narrations := []string{"只有第一句。"}

	cues := PlanCues(windows, narrations, DefaultSubtitleOptions())
	if len(cues) != 1 {
		t.Fatalf("应只生成 1 条字幕，实际 %d 条", len(cues))
	}
	if cues[0].End > 3+1e-9 {
		t.Fatalf("字幕越过了第一个窗口：%.3f", cues[0].End)
	}
}

// ---------------------------------------------------------------------------
// SRT 输出
// ---------------------------------------------------------------------------

func TestFormatSRTTime(t *testing.T) {
	cases := []struct {
		sec  float64
		want string
	}{
		{0, "00:00:00,000"},
		{1.5, "00:00:01,500"},
		{61.25, "00:01:01,250"},
		{3661.007, "01:01:01,007"},
		{-5, "00:00:00,000"},
		// 边界：59.9995 秒四舍五入到 60000 毫秒，必须是 00:01:00,000
		// 而不是非法的 00:00:60,000 或 00:00:59,1000。
		{59.9995, "00:01:00,000"},
	}
	for _, c := range cases {
		if got := formatSRTTime(c.sec); got != c.want {
			t.Errorf("formatSRTTime(%.4f) = %q，期望 %q", c.sec, got, c.want)
		}
	}
}

func TestFormatSRT(t *testing.T) {
	cues := []Cue{
		{Start: 0, End: 1.5, Text: "第一句"},
		{Start: 1.5, End: 3.25, Text: "第二句"},
	}
	got := FormatSRT(cues)

	want := "1\n00:00:00,000 --> 00:00:01,500\n第一句\n\n" +
		"2\n00:00:01,500 --> 00:00:03,250\n第二句\n\n"
	if got != want {
		t.Fatalf("SRT 输出不符：\n得到: %q\n期望: %q", got, want)
	}
	// 序号必须从 1 开始，且用逗号而非句点分隔毫秒。
	if strings.Contains(got, "00:00:00.000") {
		t.Error("毫秒分隔符必须是逗号，句点会被部分播放器静默忽略")
	}
}

// TestClampCuesToDuration 覆盖字幕裁剪。
//
// 这个函数的存在源于一个真实踩到的坑：最后一条字幕恰好结束于视频末尾时，
// 封装阶段的 -shortest 会把它压成零长 —— 字幕流还在、内容为空。
// 留出尾部边距从根上避开该组合。
func TestClampCuesToDuration(t *testing.T) {
	cues := []Cue{
		{Start: 0, End: 1, Text: "保留"},
		{Start: 1, End: 2.5, Text: "需要被裁到 1.9"},
		{Start: 3.0, End: 4.0, Text: "整条越界，丢弃"},
	}

	// 成片 2.0 秒、末尾边距 0.1 → 上限 1.9。
	got := ClampCuesToDuration(cues, 2.0, 0.1)
	if len(got) != 2 {
		t.Fatalf("应保留 2 条，实际 %d 条：%+v", len(got), got)
	}
	if math.Abs(got[1].End-1.9) > 1e-9 {
		t.Errorf("第二条应被裁到 1.9，实际 %.3f", got[1].End)
	}
	if got[1].Text != "需要被裁到 1.9" {
		t.Errorf("裁剪不应改变文本，实际 %q", got[1].Text)
	}
	for i, c := range got {
		if c.End > 1.9+1e-9 {
			t.Errorf("cue %d 终点 %.3f 仍越界", i, c.End)
		}
		if c.End <= c.Start {
			t.Errorf("cue %d 被裁成非正长度（会产生空白字幕）", i)
		}
	}
}

func TestClampCuesToDurationEdgeCases(t *testing.T) {
	cues := []Cue{{Start: 0, End: 3, Text: "内容"}}

	// 总时长未知：原样返回，不做裁剪（宁可多留也不要误删）。
	if got := ClampCuesToDuration(cues, 0, 0.1); len(got) != 1 {
		t.Errorf("总时长未知时不应裁剪，实际 %d 条", len(got))
	}
	// 边距大于总时长：全部丢弃，而不是留下零长条目。
	if got := ClampCuesToDuration(cues, 0.05, 0.1); len(got) != 0 {
		t.Errorf("边距吃掉全部时长时应返回空，实际 %+v", got)
	}
	// 负边距按 0 处理。
	if got := ClampCuesToDuration(cues, 3, -1); len(got) != 1 || got[0].End != 3 {
		t.Errorf("负边距应按 0 处理，实际 %+v", got)
	}
	// 空输入不 panic。
	if got := ClampCuesToDuration(nil, 10, 0.1); len(got) != 0 {
		t.Errorf("空输入应返回空，实际 %+v", got)
	}
}

func TestWriteSRT(t *testing.T) {
	dir := t.TempDir()
	path := dir + "/out.srt"

	if err := WriteSRT(path, nil); err == nil {
		t.Error("空字幕列表应报错而不是写出一个空文件")
	}

	cues := []Cue{{Start: 0, End: 1, Text: "内容"}}
	if err := WriteSRT(path, cues); err != nil {
		t.Fatalf("写字幕失败: %v", err)
	}
	b, err := readFile(path)
	if err != nil {
		t.Fatalf("读字幕失败: %v", err)
	}
	if !strings.Contains(string(b), "内容") {
		t.Errorf("文件内容不符：%q", string(b))
	}
}

// ---------------------------------------------------------------------------
// 按真实配音时长排布（TTS 接入后的路径）
// ---------------------------------------------------------------------------

// TestPlanCuesWithNarrationLeavesTailSilent 是本组最核心的一条。
//
// 画面 8 秒、配音只有 5 秒时，最后一句字幕**必须**在 5 秒处结束，
// 而不是被拉伸着挂到 8 秒 —— 声音早就停了、观众也早就读完了，
// 字幕却还在屏幕上，这是「字幕与语音不同步」最典型的形态。
func TestPlanCuesWithNarrationLeavesTailSilent(t *testing.T) {
	windows := []Window{{Start: 0, End: 8}}
	narrations := []string{"第一句话。第二句话。"}
	opt := SubtitleOptions{MaxCharsPerCue: 50, MinCueSec: 0.1, MaxCueSec: 100}

	cues := PlanCuesWithNarration(windows, narrations, []float64{5.0}, opt)
	if len(cues) == 0 {
		t.Fatal("应当生成字幕")
	}

	last := cues[len(cues)-1]
	if diff := last.End - 5.0; diff > 1e-6 || diff < -1e-6 {
		t.Errorf("最后一条字幕应当结束于配音结束处 5.000s，实际 %.3f s"+
			"（按窗口铺满会把末尾 3 秒的留白也算进字幕时长）", last.End)
	}

	// 反向对照：不传配音时长时，行为应与原先一致（铺满窗口）。
	fallback := PlanCuesWithNarration(windows, narrations, []float64{0}, opt)
	if got := fallback[len(fallback)-1].End; got != 8.0 {
		t.Errorf("没有配音信息时应回退成铺满窗口（8.000），实际 %.3f", got)
	}
}

// TestPlanCuesWithNarrationClampsToWindow 覆盖配音**比画面长**的情况。
//
// 字幕不能侵占下一个镜头的地盘 —— 那会让观众在两个镜头之间看到错位的文字。
// 这种情况同时是一个信号：该镜头的画面需要加长。
func TestPlanCuesWithNarrationClampsToWindow(t *testing.T) {
	windows := []Window{{Start: 0, End: 4}, {Start: 4, End: 10}}
	narrations := []string{"这句话的配音比画面长。", "第二个镜头。"}

	cues := PlanCuesWithNarration(windows, narrations, []float64{9.0, 0}, DefaultSubtitleOptions())
	for i, c := range cues {
		if c.End > 4.0+1e-9 && c.Start < 4.0 {
			t.Fatalf("cue %d [%.3f,%.3f] 跨越了镜头边界", i, c.Start, c.End)
		}
		if c.Start < -1e-9 {
			t.Fatalf("cue %d 起点为负：%.3f", i, c.Start)
		}
	}
	for _, c := range cues {
		if c.Start >= 0 && c.End <= 4.0+1e-9 && c.Start < 4.0 {
			if c.End > 4.0+1e-9 {
				t.Errorf("第一个镜头的字幕越界到了 %.3f", c.End)
			}
		}
	}
}

// TestPlanCuesWithNarrationPartial 覆盖「只有部分镜头有配音」。
//
// 现实中很容易出现：某些镜头是纯画面/环境音，本就没有旁白。
// 不能因为缺一个值就整体退化成估算路径。
func TestPlanCuesWithNarrationPartial(t *testing.T) {
	windows := []Window{{Start: 0, End: 6}, {Start: 6, End: 12}}
	narrations := []string{"有配音的镜头。", "没有配音的镜头。"}
	opt := SubtitleOptions{MaxCharsPerCue: 50, MinCueSec: 0.1, MaxCueSec: 100}

	cues := PlanCuesWithNarration(windows, narrations, []float64{2.0, 0}, opt)
	if len(cues) < 2 {
		t.Fatalf("两个镜头都应有字幕，实际 %d 条", len(cues))
	}
	// 第一个镜头：结束于配音结束处
	if cues[0].End > 2.0+1e-6 {
		t.Errorf("第一个镜头有配音（2.0s），字幕不应超过它，实际 %.3f", cues[0].End)
	}
	// 第二个镜头：无配音 → 铺满窗口
	last := cues[len(cues)-1]
	if last.End != 12.0 {
		t.Errorf("第二个镜头没有配音，应当铺满到窗口末尾 12.000，实际 %.3f", last.End)
	}
}

// TestPlanCuesWithNarrationShortNarrationMerges 覆盖「配音极短」。
//
// 配音只够放 1 条字幕时，内容必须合并而不是丢字。
func TestPlanCuesWithNarrationShortNarrationMerges(t *testing.T) {
	windows := []Window{{Start: 0, End: 10}}
	narrations := []string{"第一句。第二句。第三句。"}

	cues := PlanCuesWithNarration(windows, narrations, []float64{1.2}, DefaultSubtitleOptions())
	if len(cues) != 1 {
		t.Fatalf("1.2 秒只够一条字幕，实际 %d 条：%+v", len(cues), cues)
	}
	for _, want := range []string{"第一句", "第二句", "第三句"} {
		if !strings.Contains(cues[0].Text, want) {
			t.Errorf("合并后丢内容了：缺 %q，实际 %q", want, cues[0].Text)
		}
	}
	if cues[0].End > 1.2+1e-6 {
		t.Errorf("字幕不应超过配音结束处，实际 %.3f", cues[0].End)
	}
}
