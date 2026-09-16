package media

import (
	"context"
	"fmt"
	"strings"
)

// ---------------------------------------------------------------------------
// 局部重渲染（只重渲某几秒，而不是整镜）
// ---------------------------------------------------------------------------

// partialReplaceThreshold 是「拼接不再划算」的阈值。
//
// 当需要替换的区间已经覆盖原片大部分时，拼接要做的工作（解码 + 滤镜 + 重编码）
// 并不比直接整段替换少，而滤镜图更复杂、出错面更大。
// 超过这个比例就直接整体替换。
const partialReplaceThreshold = 0.9

// SplicePlan 描述「用一段新渲染的片段替换原片某个区间」的拼接方案。
//
// 纯数据 + 纯函数推导，因此边界条件可以完整单测 ——
// 拼接的边界（头部要不要、尾部要不要、区间越界）正是这类代码最容易出错的地方。
type SplicePlan struct {
	BaseDuration float64 // 原片时长
	PatchStart   float64 // 替换区间起点（已裁剪到 [0, BaseDuration]）
	PatchEnd     float64 // 替换区间终点（已裁剪到 [0, BaseDuration]）
	Head         float64 // 保留的前段时长；0 表示没有前段
	TailStart    float64 // 保留的后段起点（相对原片）
	Tail         float64 // 保留的后段时长；0 表示没有后段
	Segments     int     // 参与拼接的段数（1~3）
	// FullReplace 为 true 时不应拼接，直接把新片段当作整镜使用。
	FullReplace bool
	// Reason 说明为何走整体替换，用于事件与日志。
	Reason string
}

// PlanSplice 推导拼接方案。
//
// 输入是「原片时长」与「需要替换的区间」，输出保留前段/后段的具体切点。
// 区间会被裁剪到原片范围内：渲染方偶尔会给出略微越界的区间
// （例如原作 4.0 秒，请求替换 3.2~4.3），越界不应当成错误，
// 而应当裁剪成 3.2~4.0 —— 因为它的意图是明确的。
func PlanSplice(baseDuration, patchStart, patchEnd float64) SplicePlan {
	if baseDuration <= 0 {
		return SplicePlan{
			BaseDuration: baseDuration,
			FullReplace:  true,
			Segments:     1,
			Reason:       "原片时长未知或为零，无法拼接，直接整体替换",
		}
	}

	if patchEnd <= patchStart {
		return SplicePlan{
			BaseDuration: baseDuration,
			FullReplace:  true,
			Segments:     1,
			Reason:       "替换区间为空或反向，直接整体替换",
		}
	}

	// 裁剪到原片范围内。
	if patchStart < 0 {
		patchStart = 0
	}
	if patchEnd > baseDuration {
		patchEnd = baseDuration
	}
	if patchEnd <= patchStart {
		return SplicePlan{
			BaseDuration: baseDuration,
			FullReplace:  true,
			Segments:     1,
			Reason:       "替换区间裁剪后为空（完全落在原片之外），直接整体替换",
		}
	}

	patchLen := patchEnd - patchStart
	if patchLen >= baseDuration*partialReplaceThreshold {
		return SplicePlan{
			BaseDuration: baseDuration,
			PatchStart:   patchStart,
			PatchEnd:     patchEnd,
			FullReplace:  true,
			Segments:     1,
			Reason: fmt.Sprintf("替换区间占原片 %.0f%%（阈值 %.0f%%），拼接不划算，直接整体替换",
				patchLen/baseDuration*100, partialReplaceThreshold*100),
		}
	}

	plan := SplicePlan{
		BaseDuration: baseDuration,
		PatchStart:   patchStart,
		PatchEnd:     patchEnd,
	}

	// 前段：[0, patchStart)
	if patchStart > 0 {
		plan.Head = patchStart
	}
	// 后段：[patchEnd, baseDuration)
	if patchEnd < baseDuration {
		plan.TailStart = patchEnd
		plan.Tail = baseDuration - patchEnd
	}

	// 段数 = 前段 + 新片段 + 后段。
	plan.Segments = 1
	if plan.Head > 0 {
		plan.Segments++
	}
	if plan.Tail > 0 {
		plan.Segments++
	}
	return plan
}

// SavedRatio 返回拼接相比整镜重渲「省下」的时长比例。
//
// 用于在事件与日志里给出可量化的收益证据 ——
// 一个没有度量指标的优化，无法判断它是否真的生效。
func (p SplicePlan) SavedRatio() float64 {
	if p.FullReplace || p.BaseDuration <= 0 {
		return 0
	}
	return 1 - (p.PatchEnd-p.PatchStart)/p.BaseDuration
}

// BuildSpliceFilter 把拼接方案编译成 ffmpeg 的 filter_complex。
//
// 输入约定：输入 0 = 原片，输入 1 = 新渲染的替换片段。
// 两者都必须是统一规格（分辨率/帧率/像素格式/色彩范围/音轨一致），
// 由调用方负责 —— 这里不做校验，因为滤镜图阶段已经来不及补救了。
//
// 输出：[vout] 与 [aout]。
func BuildSpliceFilter(plan SplicePlan) (string, error) {
	if plan.FullReplace {
		return "", fmt.Errorf("media: 该方案为整体替换（%s），不应构建拼接滤镜图", plan.Reason)
	}
	if plan.Segments < 1 || plan.Segments > 3 {
		return "", fmt.Errorf("media: 拼接段数 %d 非法（应为 1~3）", plan.Segments)
	}

	needHead := plan.Head > 0
	needTail := plan.Tail > 0

	var sb strings.Builder

	// 原片要被切两刀（前段 + 后段）时，必须先用 split 分流：
	// 同一个输入 pad 不能被两条滤镜链重复消费，否则 ffmpeg 会直接报错。
	//
	// 音频必须用 **asplit** 而不是 split —— split 是视频专用滤镜。
	// 用错时 ffmpeg 不会说「你该用 asplit」，而是抛出一句极其误导的
	// 「Media type mismatch between the split output pad 0 (video) and
	//  the atrim input pad 0 (audio)」，让人以为是标签写错或流选错了。
	headV, headA := "0:v", "0:a"
	tailV, tailA := "0:v", "0:a"
	if needHead && needTail {
		sb.WriteString("[0:v]split=2[bv1][bv2];")
		sb.WriteString("[0:a]asplit=2[ba1][ba2];")
		headV, headA = "bv1", "ba1"
		tailV, tailA = "bv2", "ba2"
	}

	var vLabels, aLabels []string

	if needHead {
		fmt.Fprintf(&sb, "[%s]trim=start=0:end=%.3f,setpts=PTS-STARTPTS[vh];", headV, plan.Head)
		fmt.Fprintf(&sb, "[%s]atrim=start=0:end=%.3f,asetpts=PTS-STARTPTS[ah];", headA, plan.Head)
		vLabels = append(vLabels, "vh")
		aLabels = append(aLabels, "ah")
	}

	// 新片段永远参与拼接（否则本次调用没有意义）。
	fmt.Fprintf(&sb, "[1:v]setpts=PTS-STARTPTS[vp];")
	fmt.Fprintf(&sb, "[1:a]asetpts=PTS-STARTPTS[ap];")
	vLabels = append(vLabels, "vp")
	aLabels = append(aLabels, "ap")

	if needTail {
		fmt.Fprintf(&sb, "[%s]trim=start=%.3f,setpts=PTS-STARTPTS[vt];", tailV, plan.TailStart)
		fmt.Fprintf(&sb, "[%s]atrim=start=%.3f,asetpts=PTS-STARTPTS[at];", tailA, plan.TailStart)
		vLabels = append(vLabels, "vt")
		aLabels = append(aLabels, "at")
	}

	if len(vLabels) != len(aLabels) || len(vLabels) != plan.Segments {
		return "", fmt.Errorf("media: 拼接段数不一致（视频 %d、音频 %d、方案 %d）",
			len(vLabels), len(aLabels), plan.Segments)
	}

	// concat 滤镜在同时处理音视频时，输入必须**交替排列**为
	// [v0][a0][v1][a1]…，而不是 [v0][v1]…[a0][a1]…。
	// 顺序写反是这类滤镜图最常见也最难看出原因的失败：
	// ffmpeg 不会说「你顺序错了」，只会报一些关于像素格式或采样率的费解错误。
	for i := range vLabels {
		fmt.Fprintf(&sb, "[%s][%s]", vLabels[i], aLabels[i])
	}
	fmt.Fprintf(&sb, "concat=n=%d:v=1:a=1[vout][aout]", plan.Segments)

	return sb.String(), nil
}

// SpliceSegment 用一段新渲染的片段替换原片的指定区间。
//
// 与「整镜重渲」相比，代价从「渲染整镜」降到「渲染该区间 + 一次拼接」。
// 对 Manim/浏览器这类渲染成本远高于编码成本的引擎，收益非常显著。
//
// 新片段由调用方保证已是统一规格；原片同理（它本来就来自归一化流程）。
func (r *Runner) SpliceSegment(ctx context.Context, basePath, patchPath, out string, plan SplicePlan) error {
	filter, err := BuildSpliceFilter(plan)
	if err != nil {
		return err
	}

	return r.run(ctx,
		"-hide_banner", "-nostdin", "-y",
		"-i", basePath,
		"-i", patchPath,
		"-filter_complex", filter,
		"-map", "[vout]", "-map", "[aout]",
		"-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
		"-pix_fmt", "yuv420p",
		"-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
		"-color_range", "tv",
		"-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
		"-movflags", "+faststart",
		out,
	)
}
