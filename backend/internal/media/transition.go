package media

import (
	"fmt"
	"strings"
)

// ---------------------------------------------------------------------------
// 转场（跨镜头）
// ---------------------------------------------------------------------------

// TransitionType 是转场效果类型，取值直接对应 ffmpeg xfade 滤镜的 transition 名。
type TransitionType string

const (
	// TransitionNone 表示硬切：不重编码，走最快的 concat -c copy 路径。
	TransitionNone TransitionType = "none"
	// 以下均为 xfade 支持的转场，需重编码。
	TransitionFade       TransitionType = "fade"
	TransitionWipeLeft   TransitionType = "wipeleft"
	TransitionWipeRight  TransitionType = "wiperight"
	TransitionSlideLeft  TransitionType = "slideleft"
	TransitionSlideRight TransitionType = "slideright"
	TransitionCircleOpen TransitionType = "circleopen"
	TransitionDissolve   TransitionType = "dissolve"
)

// knownTransitions 是允许配置的转场集合。
//
// 白名单而不是透传：转场名最终会拼进 filtergraph 字符串，
// 直接透传用户输入等于把滤镜图交给外部配置摆布。
var knownTransitions = map[TransitionType]bool{
	TransitionNone:       true,
	TransitionFade:       true,
	TransitionWipeLeft:   true,
	TransitionWipeRight:  true,
	TransitionSlideLeft:  true,
	TransitionSlideRight: true,
	TransitionCircleOpen: true,
	TransitionDissolve:   true,
}

// ParseTransitionType 解析配置中的转场名，未知取值返回错误而不是静默降级 ——
// 「拼错一个字母，成片就悄悄变成硬切」是很难发现的故障。
func ParseTransitionType(s string) (TransitionType, error) {
	t := TransitionType(strings.ToLower(strings.TrimSpace(s)))
	if t == "" {
		return TransitionNone, nil
	}
	if !knownTransitions[t] {
		return "", fmt.Errorf("media: 未知转场类型 %q（可选：none/fade/wipeleft/wiperight/slideleft/slideright/circleopen/dissolve）", s)
	}
	return t, nil
}

// minTransitionSec 是转场时长的下限。
//
// 短于这个值的转场在观感上等同于硬切，却仍要付出重编码的全部代价，
// 因此直接按硬切处理更诚实。
const minTransitionSec = 0.1

// TransitionSpec 描述转场配置。
type TransitionSpec struct {
	Type        TransitionType
	DurationSec float64
}

// TransitionPlan 是转场方案的计算结果（纯数据，便于单测与日志留痕）。
type TransitionPlan struct {
	// Enabled 为 false 时调用方应走 concat 硬切路径。
	Enabled bool
	// Type 是转场效果类型，构建滤镜图时使用。
	Type TransitionType
	// Reason 说明未启用转场的原因，用于事件与日志 —— 静默降级是排查噩梦。
	Reason string
	// Duration 是**所有连接点统一**使用的转场时长。
	Duration float64
	// Offsets 是每个连接点的 xfade offset，长度为 片段数-1。
	// Offsets[k] 表示「已拼接的前 k+1 个片段」与第 k+1 个片段的交叠起点。
	Offsets []float64
	// OutDuration 是转场后的成片总时长。
	OutDuration float64
}

// PlanTransitions 计算转场方案。
//
// # 为什么用「统一时长」而不是逐连接点各自裁剪
//
// 直觉做法是每个连接点取 min(配置值, 前后片段时长)，但那样会出现
// 「1.2 秒的转场接 0.3 秒的转场」这种长短不一的效果，观感上非常廉价。
// 这里取**全局最小片段时长**作为统一上限：所有连接点时长一致，
// 代价是转场时长会被最短的那个片段压住 —— 对科普视频而言这是可接受的，
// 因为分镜时长本来就被导演智能体约束在相近区间。
//
// # offset 的推导（这次改动最容易写错的地方）
//
// 设片段时长 d[0..n-1]，统一转场时长 T，令 L[k] 为拼接完前 k+1 个片段后的时长：
//
//	L[0] = d[0]
//	L[k] = L[k-1] + d[k] - T
//
// 第 k 个连接点（把 d[k] 接在已拼接结果之后）的 offset 是**前一段结果的末尾
// 往前推 T**，即 offset[k] = L[k-1] - T。写成「d[k-1] 相关的简单式子」是错的：
// 一旦前面已经发生过转场，L[k-1] 就不再等于 d[k-1]。
// 这是 xfade 链式拼接最典型的错误，会让第二段之后的转场位置全部漂移。
func PlanTransitions(durations []float64, spec TransitionSpec) TransitionPlan {
	n := len(durations)
	if n == 0 {
		return TransitionPlan{Enabled: false, Reason: "没有可拼接的片段"}
	}
	if n == 1 {
		return TransitionPlan{Enabled: false, Reason: "只有一个片段，无需转场"}
	}
	if spec.Type == "" || spec.Type == TransitionNone {
		return TransitionPlan{Enabled: false, Reason: "转场类型为 none（硬切）"}
	}

	// 统一转场上限：不能超过任何一个片段，否则 xfade 的 offset 会越过前一段的末尾。
	minDur := durations[0]
	total := 0.0
	for _, d := range durations {
		if d < minDur {
			minDur = d
		}
		total += d
	}

	t := spec.DurationSec
	if t <= 0 {
		return TransitionPlan{Enabled: false, Reason: "转场时长为 0"}
	}
	// 上限取**最短片段的一半**，而不是最短片段本身。
	//
	// 取「最短片段」看似更宽松，但会有两个后果：
	//  1. 当某个片段的时长恰好等于转场时长时，它从头到尾都处在转场交叠中，
	//     没有任何一帧是独自出现的 —— 这个镜头等于没拍；
	//  2. 成片时间轴上该片段的字幕窗口长度会退化为 0。
	// 取一半就保证每个片段至少有半个身位独自出现，窗口长度恒为正。
	if half := minDur / 2; t > half {
		t = half
	}
	if t < minTransitionSec {
		return TransitionPlan{
			Enabled: false,
			Reason: fmt.Sprintf("最短片段仅 %.3fs，不足以承载转场（下限 %.2fs），降级为硬切",
				minDur, minTransitionSec),
		}
	}

	offsets := make([]float64, 0, n-1)
	cursor := durations[0] // L[k-1]
	for k := 1; k < n; k++ {
		offsets = append(offsets, cursor-t)
		cursor = cursor + durations[k] - t
	}

	return TransitionPlan{
		Enabled:     true,
		Type:        spec.Type,
		Duration:    t,
		Offsets:     offsets,
		OutDuration: cursor,
	}
}

// BuildXFadeFilter 把转场方案编译成 ffmpeg 的 filter_complex 字符串。
//
// 输入约定：第 i 个输入文件同时提供 [i:v] 视频与 [i:a] 音频，
// 且**已经过 Normalize**，分辨率、帧率、像素格式、时基完全一致 ——
// 这是 xfade / acrossfade 能工作的前提，不满足时会得到花屏或直接报错。
//
// 输出：[vout] 与 [aout]。
func BuildXFadeFilter(plan TransitionPlan, n int) (string, error) {
	if !plan.Enabled {
		return "", fmt.Errorf("media: 转场方案未启用，不应调用 BuildXFadeFilter")
	}
	if n < 2 {
		return "", fmt.Errorf("media: 转场至少需要 2 个片段，实际 %d", n)
	}
	if len(plan.Offsets) != n-1 {
		return "", fmt.Errorf("media: 转场 offset 数量 %d 与片段数 %d 不匹配", len(plan.Offsets), n)
	}

	var sb strings.Builder

	// setpts=PTS-STARTPTS 把每个输入的时基归零。
	// 不归零时 xfade 的 offset 是相对**各自的**时间轴解释的，
	// 只要某个片段的起始 PTS 不是 0，转场位置就会整体偏移。
	for i := 0; i < n; i++ {
		fmt.Fprintf(&sb, "[%d:v]setpts=PTS-STARTPTS[v%d];", i, i)
		fmt.Fprintf(&sb, "[%d:a]asetpts=PTS-STARTPTS[a%d];", i, i)
	}

	// 视频链：逐段 xfade。
	prev := "v0"
	for k := 1; k < n; k++ {
		out := fmt.Sprintf("vx%d", k)
		fmt.Fprintf(&sb, "[%s][v%d]xfade=transition=%s:duration=%.3f:offset=%.3f[%s];",
			prev, k, plan.Type, plan.Duration, plan.Offsets[k-1], out)
		prev = out
	}
	fmt.Fprintf(&sb, "[%s]format=yuv420p[vout];", prev)

	// 音频链：逐段 acrossfade。
	//
	// 音频必须用**同样的**时长做交叠，否则每经过一个转场，音轨就会比画面
	// 多出 T 秒，几个镜头之后音画彻底错位。
	prevA := "a0"
	for k := 1; k < n; k++ {
		out := fmt.Sprintf("ax%d", k)
		fmt.Fprintf(&sb, "[%s][a%d]acrossfade=d=%.3f:c1=tri:c2=tri[%s];", prevA, k, plan.Duration, out)
		prevA = out
	}
	fmt.Fprintf(&sb, "[%s]anull[aout]", prevA)

	return sb.String(), nil
}
