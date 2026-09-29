package media

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
)

// PreviewSpec 描述一次效果预览要渲染什么。
//
// 预览的**全部意义**在于"所见即所得"，因此这里的每一步都必须复用成片的
// 真实代码路径：背景用 BackgroundFilter、调色/淡入淡出/字幕样式走同一个
// PostProcess、配乐走同一个 BuildBgmTrack + MixSoundtrack。
// 只要有一处另写一套，"预览好看、成片不同"就会立刻发生，而那种不一致
// 比没有预览更糟 —— 用户会按着预览去调参数。
type PreviewSpec struct {
	// BackgroundStyle 是背景预设 id（见 BackgroundStyleIDs）。
	BackgroundStyle string
	BackgroundColor string
	PrimaryColor    string
	Width           int
	Height          int
	FPS             int
	// DurationSec 是预览时长。取几秒即可：效果都是逐帧/逐段的，
	// 渲染整片只会让人等，而不会多看出什么。
	DurationSec float64
	// Post 是后期效果。SubtitleStyle 在这里才有意义（预览会烧录字幕）。
	Post PostOptions
	// BGM 为空表示预览不带配乐。
	BGM          *BgmSpec
	LoudnessLUFS float64
	// SampleText 是预览里烧录的那行示例字幕。
	SampleText string
}

// DefaultPreviewDurationSec 是缺省的预览时长。
const DefaultPreviewDurationSec = 4.0

// RenderEffectsPreview 渲染一段效果预览，产物写到 out。
//
// 流程与成片一致：**先出画面，再叠后期**。
//   - 画面：背景预设 + 测试卡（灰阶梯 / 三色块 / 主色条）；
//   - 后期：同一个 PostProcess（调色 + 淡入淡出 + 烧录字幕）；
//   - 配乐：同一个 BuildBgmTrack + MixSoundtrack，再交给同一个 MuxFinal。
func (r *Runner) RenderEffectsPreview(ctx context.Context, workDir, out string, spec PreviewSpec) error {
	if spec.DurationSec <= 0 {
		spec.DurationSec = DefaultPreviewDurationSec
	}
	if spec.Width <= 0 || spec.Height <= 0 || spec.FPS <= 0 {
		return fmt.Errorf("media: 预览尺寸/帧率非法（%dx%d@%d）",
			spec.Width, spec.Height, spec.FPS)
	}
	if workDir == "" {
		return fmt.Errorf("media: 预览工作目录不能为空")
	}
	if err := os.MkdirAll(workDir, 0o755); err != nil {
		return fmt.Errorf("media: 创建预览工作目录失败: %w", err)
	}

	// 阶段一：背景 + 测试卡。
	chain, err := BackgroundFilter(spec.BackgroundStyle,
		spec.BackgroundColor, spec.PrimaryColor, spec.Width, spec.Height, spec.FPS)
	if err != nil {
		return err
	}
	testCard := TestCardFilter(spec.BackgroundColor, spec.PrimaryColor, spec.Width, spec.Height)
	src := filepath.Join(workDir, "preview_src.mp4")
	if err := r.run(ctx,
		"-hide_banner", "-nostdin", "-y",
		"-f", "lavfi", "-i", chain,
		"-vf", "format=yuv420p,"+testCard,
		"-t", fmt.Sprintf("%.3f", spec.DurationSec),
		"-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
		"-color_range", "tv",
		"-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
		"-movflags", "+faststart",
		src,
	); err != nil {
		return fmt.Errorf("media: 生成预览底图失败: %w", err)
	}

	// 阶段二：示例字幕。写成 SRT 走真实的烧录路径，而不是"画一行字"。
	post := spec.Post
	if post.BurnSubtitlePath == "" {
		text := spec.SampleText
		if text == "" {
			text = "预览字幕示例 Preview"
		}
		srt := filepath.Join(workDir, "preview.srt")
		if werr := WriteSRT(srt, []Cue{{
			Start: spec.DurationSec * 0.12,
			End:   spec.DurationSec * 0.92,
			Text:  text,
		}}); werr != nil {
			return fmt.Errorf("media: 写入预览字幕失败: %w", werr)
		}
		post.BurnSubtitlePath = srt
	}

	// 阶段三：后期。NeedsPostProcess 为假时不空跑一次编码 —— 与成片同一套判断。
	videoPath := src
	if NeedsPostProcess(post) {
		graded := filepath.Join(workDir, "preview_post.mp4")
		if perr := r.PostProcess(ctx, src, graded, post, spec.DurationSec); perr != nil {
			return fmt.Errorf("media: 预览后期处理失败: %w", perr)
		}
		videoPath = graded
	}

	// 阶段四：配乐（没有就到此为止，不必为了统一而多跑两次封装）。
	if spec.BGM == nil || spec.BGM.Path == "" {
		return moveFile(videoPath, out)
	}
	bgmTrack := filepath.Join(workDir, "preview_bgm.m4a")
	if berr := r.BuildBgmTrack(ctx, spec.BGM.Path, bgmTrack, spec.DurationSec, *spec.BGM); berr != nil {
		return fmt.Errorf("media: 预览配乐配轨失败: %w", berr)
	}
	// 旁白为空：预览里没有 TTS，配乐直接做响度归一化。
	mixed := filepath.Join(workDir, "preview_audio.m4a")
	if merr := r.MixSoundtrack(ctx, "", bgmTrack, mixed, spec.LoudnessLUFS); merr != nil {
		return fmt.Errorf("media: 预览混音失败: %w", merr)
	}
	return r.MuxFinal(ctx, videoPath, mixed, "", out)
}

// moveFile 把产物挪到目标位置；跨盘时退回复制。
//
// 不直接用 os.Rename 是因为预览目录与工作目录可能不在同一个盘上，
// 而 Windows 上跨盘 Rename 会直接失败。
func moveFile(src, dst string) error {
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return fmt.Errorf("media: 创建预览输出目录失败: %w", err)
	}
	if err := os.Rename(src, dst); err == nil {
		return nil
	}
	in, err := os.Open(src)
	if err != nil {
		return fmt.Errorf("media: 打开预览产物失败: %w", err)
	}
	defer in.Close()
	outFile, err := os.Create(dst)
	if err != nil {
		return fmt.Errorf("media: 创建预览产物失败: %w", err)
	}
	defer outFile.Close()
	if _, err := outFile.ReadFrom(in); err != nil {
		return fmt.Errorf("media: 复制预览产物失败: %w", err)
	}
	return outFile.Sync()
}
