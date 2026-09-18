package media

import (
	"fmt"
	"os"
	"strings"
	"unicode"
)

// ---------------------------------------------------------------------------
// 字幕时间轴
// ---------------------------------------------------------------------------

// Window 是一个镜头在**成片时间轴**上的可见区间（秒）。
type Window struct {
	Start float64
	End   float64
}

// Duration 返回窗口长度。
func (w Window) Duration() float64 { return w.End - w.Start }

// Cue 是一条字幕。
type Cue struct {
	Start float64
	End   float64
	Text  string
}

// PlanShotWindows 计算每个镜头在成片时间轴上的可见区间。
//
// # 为什么不能简单地把片段时长累加
//
// 启用转场后成片比片段之和**短** (n-1)×T —— 转场是交叠而不是插入。
// 如果字幕按「累加原始时长」定位，每经过一个转场字幕就往后偏 T 秒，
// 几个镜头之后就会整体错位到画面前面去。这类错误在片头看不出来，
// 越往后越明显，是最容易被漏掉的一类 bug。
//
// 推导：设 offset[k] 为第 k 个连接点的 xfade 起点（由 PlanTransitions 给出），
// 则镜头 k 的可见区间是 [boundary[k], boundary[k+1])，其中
//
//	boundary[0] = 0
//	boundary[k] = offset[k-1]   (k >= 1)
//	boundary[n] = 成片总时长
//
// 即「下一个镜头开始淡入的时刻」就是「当前镜头开始让位给它的时刻」。
// 这样得到的窗口**首尾相接、互不重叠、完整覆盖整条时间轴**。
//
// 不启用转场时退化为大家熟悉的累加：boundary[k] = d[0]+…+d[k-1]。
func PlanShotWindows(durations []float64, plan TransitionPlan) []Window {
	n := len(durations)
	if n == 0 {
		return nil
	}

	boundaries := make([]float64, n+1)
	boundaries[0] = 0
	for k := 1; k < n; k++ {
		if plan.Enabled && k-1 < len(plan.Offsets) {
			boundaries[k] = plan.Offsets[k-1]
		} else {
			boundaries[k] = boundaries[k-1] + durations[k-1]
		}
	}

	if plan.Enabled {
		boundaries[n] = plan.OutDuration
	} else {
		boundaries[n] = boundaries[n-1] + durations[n-1]
	}

	windows := make([]Window, n)
	for k := 0; k < n; k++ {
		windows[k] = Window{Start: boundaries[k], End: boundaries[k+1]}
	}
	return windows
}

// ---------------------------------------------------------------------------
// 断句与时长分配
// ---------------------------------------------------------------------------

// SubtitleOptions 是字幕生成参数。
type SubtitleOptions struct {
	// MaxCharsPerCue 是单条字幕的长度上限（按「语音权重单位」计，见 speechWeight）。
	// 超长会硬拆，避免一行字幕占满整个画面宽度。
	MaxCharsPerCue int
	// MinCueSec 是单条字幕的最短显示时长。
	// 低于这个值的字幕会在观众读完之前就闪走，观感上等同于没有。
	MinCueSec float64
	// MaxCueSec 是单条字幕的最长显示时长，超过就拆成多条。
	MaxCueSec float64
}

// DefaultSubtitleOptions 返回默认参数。
//
// 上限 18 个字：中文科普视频里，一行字幕超过 18 字在手机上就会被迫折行两次，
// 遮挡画面且破坏阅读节奏。
func DefaultSubtitleOptions() SubtitleOptions {
	return SubtitleOptions{
		MaxCharsPerCue: 18,
		MinCueSec:      0.8,
		MaxCueSec:      8.0,
	}
}

// isCJK 判断是否为中日韩表意文字或全角标点。
func isCJK(r rune) bool {
	return unicode.Is(unicode.Han, r) ||
		unicode.Is(unicode.Hiragana, r) ||
		unicode.Is(unicode.Katakana, r) ||
		unicode.Is(unicode.Hangul, r)
}

// speechWeight 估算一段文本的「朗读耗时权重」。
//
// 需要一个跨语言的统一度量，原因是导演智能体产出的 narration 常常中英混排
// （「傅里叶变换（Fourier Transform）的实现」）。若只数字符个数，
// 英文部分会被严重高估 —— 一个 12 字母的单词只占约 1.5 个汉字的时间。
//
// 标定：一个汉字 ≈ 一个音节；一个英文单词 ≈ 1.6 个音节。
// 系数不追求精确（真实节奏要等 TTS 接上后按音频时长校正），
// 只要在**同一镜头内部**能把时间按相对长度分对就够了。
func speechWeight(s string) float64 {
	var cjk float64
	var wordRunes int
	inWord := false

	flush := func() {
		if inWord && wordRunes > 0 {
			cjk += 1.6
		}
		wordRunes = 0
		inWord = false
	}

	for _, r := range s {
		switch {
		case isCJK(r):
			flush()
			cjk++
		case unicode.IsLetter(r) || unicode.IsDigit(r):
			inWord = true
			wordRunes++
		default:
			flush()
		}
	}
	flush()

	if cjk < 1 {
		// 全是标点或空串：给一个最小权重，避免参与分配时除零。
		return 1
	}
	return cjk
}

// splitSentences 按句末标点断句。
//
// 需要小心的特例：英文句点同时是小数点与缩写点。
// 「准确率 3.14」如果在 3 与 14 之间断开，字幕会变成两条无意义的碎片。
// 因此句点只有**后面不是数字**时才当作句子边界。
func splitSentences(text string) []string {
	var out []string
	var cur []rune
	runes := []rune(strings.TrimSpace(text))

	flush := func() {
		s := strings.TrimSpace(string(cur))
		if s != "" {
			out = append(out, s)
		}
		cur = cur[:0]
	}

	for i, r := range runes {
		cur = append(cur, r)

		switch r {
		case '\n':
			flush()
			continue
		case '。', '！', '？', '；', '!', '?', ';':
			flush()
			continue
		}
		if r == '.' {
			// 小数点或缩写：后面紧跟数字时不作为句子边界。
			if i+1 < len(runes) && unicode.IsDigit(runes[i+1]) {
				continue
			}
			flush()
		}
	}
	flush()
	return out
}

// splitLongSegment 把过长的句子进一步拆短。
//
// 两级策略：先找次级停顿（逗号、顿号、冒号）拆；实在拆不动才按字数硬切。
// 硬切是最后手段 —— 它可能把词切开，但总好过一条占满屏幕的字幕。
func splitLongSegment(seg string, maxChars int) []string {
	if speechWeight(seg) <= float64(maxChars) {
		return []string{seg}
	}

	var out []string
	var cur []rune
	runes := []rune(seg)

	flush := func() {
		s := strings.TrimSpace(string(cur))
		if s != "" {
			out = append(out, s)
		}
		cur = cur[:0]
	}

	for _, r := range runes {
		cur = append(cur, r)
		isSecondary := r == '，' || r == '、' || r == '：' || r == ',' || r == ':'
		if isSecondary && speechWeight(string(cur)) >= float64(maxChars)/2 {
			flush()
			continue
		}
		if speechWeight(string(cur)) >= float64(maxChars) {
			flush()
		}
	}
	flush()

	if len(out) == 0 {
		return []string{seg}
	}
	return out
}

// PlanCues 把每个镜头的画外音分配到它的可见窗口里。
//
// 分配规则：同一镜头内按 speechWeight 比例切分窗口时长。
// 长句多分时间、短句少分，从而让字幕与语速大致同步。
//
// **这是「该镜头还没有真实配音」时的估算路径**：它假设画外音正好铺满整个窗口。
// 一旦拿到了配音时长，就应当改用 PlanCuesWithNarration。
//
// 两个必须处理的边界：
//  1. **窗口太短**：当窗口时长不足以让每条字幕都达到 MinCueSec 时，
//     继续拆分只会得到一堆一闪而过的碎片，因此合并成更少的条数；
//  2. **单条太长**：超过 MaxCueSec 的条数会被继续拆，
//     否则会出现「一句话在屏幕上挂了 10 秒」。
func PlanCues(windows []Window, narrations []string, opt SubtitleOptions) []Cue {
	return planCues(windows, narrations, nil, opt)
}

// PlanCuesWithNarration 与 PlanCues 相同，但字幕按**真实配音时长**排布。
//
// narrationSec[i] 是第 i 个镜头配音的实际时长（用 ffprobe 探测，见 Runner.Probe）。
// `<= 0` 表示该镜头没有配音，回退成「铺满整个窗口」—— 也就是 PlanCues 的行为，
// 因此「部分镜头有配音、部分没有」也能正常工作。
//
// **为什么必须按真实时长而不是按文本长度估算**：画面时长与配音时长不相等。
// 一个 8 秒的镜头可能只有 5 秒旁白，剩下 3 秒是留白。按窗口铺满会把最后一句字幕
// **拉伸着挂在屏幕上 3 秒** —— 声音早就停了、观众也早就读完了，字幕却还在。
// 这是「字幕与语音不同步」最典型的形态，而它在只看文本的单测里永远暴露不出来。
//
// 反过来，配音比画面长时字幕会被夹到窗口末尾（字幕不能侵占下一个镜头），
// 这同时是一个信号：**该镜头的画面需要加长**。
func PlanCuesWithNarration(
	windows []Window,
	narrations []string,
	narrationSec []float64,
	opt SubtitleOptions,
) []Cue {
	return planCues(windows, narrations, narrationSec, opt)
}

func planCues(
	windows []Window,
	narrations []string,
	narrationSec []float64,
	opt SubtitleOptions,
) []Cue {
	if opt.MaxCharsPerCue <= 0 || opt.MinCueSec <= 0 || opt.MaxCueSec <= 0 {
		opt = DefaultSubtitleOptions()
	}

	var cues []Cue
	n := len(windows)
	if len(narrations) < n {
		n = len(narrations)
	}

	for i := 0; i < n; i++ {
		w := windows[i]
		text := strings.TrimSpace(narrations[i])
		if text == "" || w.Duration() <= 0 {
			continue
		}

		// 字幕真正排布的区间：有配音就只覆盖配音那一段，末尾留白。
		span := w
		if i < len(narrationSec) && narrationSec[i] > 0 {
			end := w.Start + narrationSec[i]
			if end > w.End {
				end = w.End // 配音超出画面：夹到窗口末尾，不侵占下一个镜头
			}
			if end > span.Start {
				span.End = end
			}
		}

		var segs []string
		for _, s := range splitSentences(text) {
			segs = append(segs, splitLongSegment(s, opt.MaxCharsPerCue)...)
		}
		if len(segs) == 0 {
			continue
		}

		// 条数上限：这段时间能容纳的字幕条数（受最短显示时长约束）。
		// 例如 2 秒的窗口、最短 0.8 秒，最多放 2 条。
		maxCues := int(span.Duration() / opt.MinCueSec)
		if maxCues < 1 {
			maxCues = 1
		}
		segs = mergeToAtMost(segs, maxCues)

		cues = append(cues, allocate(span, segs)...)
	}

	return cues
}

// mergeToAtMost 把片段合并到不超过 max 条，尽量合并最短的相邻对。
//
// 合并的是**文本**：字幕条数变少但内容不丢，只是每条更长。
// 丢内容比显示得挤更不可接受 —— 观众至少能读完。
func mergeToAtMost(segs []string, max int) []string {
	if max < 1 {
		max = 1
	}
	out := make([]string, len(segs))
	copy(out, segs)

	for len(out) > max {
		// 找权重和最小的一对相邻片段合并。
		best, bestW := 0, -1.0
		for i := 0; i+1 < len(out); i++ {
			w := speechWeight(out[i]) + speechWeight(out[i+1])
			if bestW < 0 || w < bestW {
				best, bestW = i, w
			}
		}
		out[best] = out[best] + out[best+1]
		out = append(out[:best+1], out[best+2:]...)
	}
	return out
}

// allocate 把窗口时长按权重比例分配给各片段，并落成字幕条。
func allocate(w Window, segs []string) []Cue {
	weights := make([]float64, len(segs))
	total := 0.0
	for i, s := range segs {
		weights[i] = speechWeight(s)
		total += weights[i]
	}
	if total <= 0 {
		total = float64(len(segs))
	}

	winDur := w.Duration()
	cues := make([]Cue, 0, len(segs))
	cursor := w.Start
	for i, s := range segs {
		share := winDur * weights[i] / total
		end := cursor + share
		if i == len(segs)-1 {
			// 最后一条对齐到窗口末尾：浮点累加会留零点几毫秒的缝，
			// 缝隙本身无害，但会让「字幕总时长 == 窗口时长」这类断言失效，
			// 也让成片末尾的最后一句话可能因取整而消失。
			end = w.End
		}
		cues = append(cues, Cue{Start: cursor, End: end, Text: s})
		cursor = end
	}
	return cues
}

// ---------------------------------------------------------------------------
// SRT 输出
// ---------------------------------------------------------------------------

// FormatSRT 把字幕条渲染成 SRT 文本。
//
// SRT 的时间格式是 `HH:MM:SS,mmm`（逗号，不是句点）。
// 写成句点是很多播放器会静默忽略整条字幕的原因之一。
func FormatSRT(cues []Cue) string {
	var sb strings.Builder
	for i, c := range cues {
		fmt.Fprintf(&sb, "%d\n%s --> %s\n%s\n\n", i+1,
			formatSRTTime(c.Start), formatSRTTime(c.End), c.Text)
	}
	return sb.String()
}

// formatSRTTime 把秒格式化成 SRT 时间戳。
func formatSRTTime(sec float64) string {
	if sec < 0 {
		sec = 0
	}
	// 先转成毫秒整数再拆分，避免「59.9995 秒」被格式化成 `00:00:59,1000`
	// 这种非法时间戳（毫秒字段必须有且仅有 3 位）。
	totalMs := int64(sec*1000 + 0.5)
	h := totalMs / 3600000
	totalMs %= 3600000
	m := totalMs / 60000
	totalMs %= 60000
	s := totalMs / 1000
	ms := totalMs % 1000
	return fmt.Sprintf("%02d:%02d:%02d,%03d", h, m, s, ms)
}

// WriteSRT 把字幕写入文件。
func WriteSRT(path string, cues []Cue) error {
	if len(cues) == 0 {
		return fmt.Errorf("media: 没有可写入的字幕内容")
	}
	if err := os.WriteFile(path, []byte(FormatSRT(cues)), 0o644); err != nil {
		return fmt.Errorf("media: 写入字幕文件失败 %s: %w", path, err)
	}
	return nil
}

// ClampCuesToDuration 把字幕裁剪到成片时长之内，并在末尾留出安全边距。
//
// 两个作用：
//
//  1. **正确性**：成片真实时长（探测所得）可能略短于方案预测的时长，
//     裁剪掉越界的字幕，避免出现「字幕挂在片子结束之后」。
//  2. **健壮性**：结尾边距不只是美观问题。当最后一条字幕恰好结束于
//     视频末尾时，封装阶段的时长裁剪逻辑会把它压成零长 ——
//     字幕流还在，内容却是空的。留出边距从根上避开这个退化组合。
//
// 被裁掉的字幕会被丢弃而不是变成零长条目：零长字幕在播放器里
// 表现为一条莫名闪过的空白字幕。
func ClampCuesToDuration(cues []Cue, totalSec, tailMargin float64) []Cue {
	if totalSec <= 0 {
		return cues
	}
	if tailMargin < 0 {
		tailMargin = 0
	}
	limit := totalSec - tailMargin
	if limit <= 0 {
		return nil
	}

	out := make([]Cue, 0, len(cues))
	for _, c := range cues {
		if c.Start >= limit {
			continue
		}
		if c.End > limit {
			c.End = limit
		}
		if c.End <= c.Start {
			continue
		}
		out = append(out, c)
	}
	return out
}
