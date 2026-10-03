// 视频后期效果与配乐混音。
//
// 这一层回答的是「成片好不好看/好不好听」里**与内容无关**的那一半：
// 调色、片头片尾淡入淡出、可选烧录字幕，以及背景音乐的配轨与混音。
//
// 两条贯穿全文件的设计原则：
//
//  1. **默认零成本**。不请求任何效果时，调用方**一次 ffmpeg 都不该多跑**
//     （见 `NeedsPostProcess`）。调色与烧录字幕都必须重编码，而重编码既花时间
//     又损画质，绝不能因为"顺手"就给每个任务都加上。
//
//  2. **规划与执行分离**。滤镜串由纯函数算出来（`PlanGrade` / `PlanVideoChain`），
//     ffmpeg 只负责执行。滤镜串拼错时 ffmpeg 的报错往往含糊（`Invalid argument`
//     之类），能脱离进程单测规划逻辑，是这里唯一划得来的做法。
package media

import (
	"context"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

// ---------------------------------------------------------------------------
// 规格
// ---------------------------------------------------------------------------

// GradeSpec 是调色方案。Strength 为 0 时按 1 处理（见 PlanGrade 的说明）。
type GradeSpec struct {
	Name     string  `json:"name,omitempty"`
	Strength float64 `json:"strength,omitempty"`
}

// FadeSpec 是片头/片尾淡入淡出（秒）。两者都为 0 表示不做。
type FadeSpec struct {
	InSec  float64 `json:"in_sec,omitempty"`
	OutSec float64 `json:"out_sec,omitempty"`
}

// BgmSpec 是背景音乐配置。
//
// Loop 缺省应为 true：短片当 BGM 是常态，不循环会让后半段静音 ——
// 而"后半段没声音"是个不会报错的缺陷，观众只会觉得"配乐没了"。
type BgmSpec struct {
	Path       string  `json:"path,omitempty"`
	VolumeDB   float64 `json:"volume_db,omitempty"`
	Loop       bool    `json:"loop,omitempty"`
	FadeInSec  float64 `json:"fade_in_sec,omitempty"`
	FadeOutSec float64 `json:"fade_out_sec,omitempty"`
}

// PostOptions 是后期处理的开关集合。
type PostOptions struct {
	Grade GradeSpec `json:"grade,omitempty"`
	Fade  FadeSpec  `json:"fade,omitempty"`
	// BurnSubtitlePath 非空时把字幕烧进画面（而不是只挂软字幕）。
	// 代价是必须重编码，好处是任何播放器都一定能看到字幕。
	BurnSubtitlePath string `json:"-"`
	// SubtitleStyle 只在烧录字幕时生效：软字幕的样式由播放器决定，
	// 容器里存不下，因此"给软字幕设样式"是一个不会报错的空操作。
	SubtitleStyle SubtitleStyle `json:"subtitle_style,omitempty"`
	// FrameHeight 是成片高度（像素），用于推算字幕的自动字号与底边距。
	FrameHeight int `json:"-"`
	// LoudnessLUFS 是混音后的整体响度目标。0 表示用缺省值。
	LoudnessLUFS float64 `json:"loudness_lufs,omitempty"`
}

// SubtitleStyle 是烧录字幕的样式。
//
// 各字段为 0/空表示"自动"：自动值按画面高度推算，这样同一套配置
// 在 720p 与 1080p 上观感一致，而不必让用户分别配一遍。
type SubtitleStyle struct {
	// FontSize 是字号（成片像素）。0 = 自动（画面高度 / 24）。
	FontSize int `json:"font_size,omitempty"`
	// PrimaryColor 是字色（#RRGGBB）。空 = 白色。
	PrimaryColor string `json:"primary_color,omitempty"`
	// OutlineWidth 是描边宽度。0 = 自动（按字号推算）。
	//
	// 描边不是装饰：字幕压在浅色画面上时，没有描边就是一片糊。
	OutlineWidth float64 `json:"outline_width,omitempty"`
	// MarginV 是字幕距画面底边的像素。0 = 自动（画面高度 / 18）。
	MarginV int `json:"margin_v,omitempty"`
}

// 字幕自动样式的推算基准。
const (
	// subtitleFontDivisor：1080p 下得到 45px，是科普视频的常见字号。
	subtitleFontDivisor = 30
	// subtitleMarginDivisor：1080p 下得到 60px 底边距。
	subtitleMarginDivisor = 18
	minSubtitleFontSize   = 12
	maxSubtitleFontSize   = 200
)

// srtToAssPlayResY 是 libass 把 SRT 转成 ASS 时使用的**默认脚本高度**。
//
// 这是个必须知道的事实，否则字号会错得离谱：ASS 的 `FontSize`/`Outline`/`MarginV`
// 都是**脚本坐标**，而 libass 对 SRT 沿用 ASS 的传统默认 PlayResY=288。
// 于是 1080p 下所有值被乘以 `1080/288 ≈ 3.75` —— 请求 45px 实际渲染出约 169px 的字
// （实测：6 个汉字宽约 800px）。
//
// 也就是说 `effects.subtitle_style.font_size` 声称的"成片像素"，必须经过这层换算
// 才成立。实测对照：同一句 6 个汉字，SRT 路径每字约 133px，而自带
// `PlayResY=1080` 的 ASS 每字约 36px。
//
// 更彻底的修法是直接生成带 PlayRes 的 ASS（那就不需要这层换算），
// 这里先按最小改动把"像素"这个语义做对。
const srtToAssPlayResY = 288.0

// toScriptUnit 把**成片像素**换算成 ASS 的脚本坐标。
//
// frameHeight <= 0 时按 1:1 处理：推不出比例时不做换算，好过瞎乘一个系数。
func toScriptUnit(px float64, frameHeight int) float64 {
	if frameHeight <= 0 {
		return px
	}
	return px * srtToAssPlayResY / float64(frameHeight)
}

// PlanSubtitleStyle 把字幕样式翻成 libass 的 `force_style` 串。
//
// **两个坑都在这里被挡住**：
//
//  1. 颜色字节序：ASS 用的是 `&HAABBGGRR` —— 与 `#RRGGBB` 相比红蓝是**反的**。
//     照直觉写成 `&H00RRGGBB` 不会报错，只会让红色显示成蓝色。
//  2. 字号单位：`FontSize` 是**脚本坐标**而不是像素（见 srtToAssPlayResY）。
//     少了这层换算，界面里填 36 会渲染成约 135 —— 用户只会觉得"怎么调都太大"。
func PlanSubtitleStyle(s SubtitleStyle, frameHeight int) (string, error) {
	if frameHeight <= 0 {
		// 没有画面高度就推不出自动值；退回 1080p 的常见值，
		// 而不是产出空样式让字幕变成 libass 的默认大小（通常过大）。
		frameHeight = 1080
	}

	// 先在**像素**口径上把三个值算清楚并夹取。
	// 顺序很重要：换算之后再夹取会夹错（脚本单位的数比像素小 3.75 倍，
	// 拿像素区间去夹会把本该 12px 的字夹成 45px）。
	fontSizePx := s.FontSize
	if fontSizePx <= 0 {
		fontSizePx = frameHeight / subtitleFontDivisor
	}
	fontSizePx = clampInt(fontSizePx, minSubtitleFontSize, maxSubtitleFontSize)

	outlinePx := s.OutlineWidth
	if outlinePx <= 0 {
		// 描边与字号成正比：字越大，细描边就越显得没用。
		outlinePx = math.Max(1, math.Round(float64(fontSizePx)/18))
	}
	if outlinePx > 8 {
		return "", fmt.Errorf("media: 字幕描边过宽（%.1f，上限 8）", outlinePx)
	}

	marginPx := s.MarginV
	if marginPx <= 0 {
		marginPx = frameHeight / subtitleMarginDivisor
	}
	if marginPx < 0 || marginPx > frameHeight {
		return "", fmt.Errorf("media: 字幕底边距越界（%d，画面高 %d）", marginPx, frameHeight)
	}

	colour := s.PrimaryColor
	if strings.TrimSpace(colour) == "" {
		colour = "#FFFFFF"
	}
	ass, err := assColour(colour)
	if err != nil {
		return "", err
	}

	// 最后统一换算到脚本坐标。三者都要换 —— 只换字号会让描边与底边距在
	// 720p/1080p 上表现不一致，而那种问题看起来像"随机"。
	//
	// 字号与底边距用**浮点**输出而不是取整：脚本单位取整会引入量化误差，
	// 而它回头会被乘以 frameHeight/288 —— 2160p 下 ±0.5 单位就是 ±3.75px。
	// 实测取整后 2160p 的目标 72px 变成 75px。libass 能解析浮点字号。
	return fmt.Sprintf("FontSize=%.2f,PrimaryColour=%s,Outline=%.2f,MarginV=%.2f",
		toScriptUnit(float64(fontSizePx), frameHeight),
		ass,
		toScriptUnit(outlinePx, frameHeight),
		toScriptUnit(float64(marginPx), frameHeight),
	), nil
}

// assColour 把 `#RRGGBB` 转成 ASS 的 `&HAABBGGRR`。
//
// 刻意**不接受**省略 `#` 的写法（如 `ABCDEF`）：domain 层的校验要求必须带 `#`，
// 两处一旦松紧不一，就会出现"接口拒绝了、渲染器其实能接受"这种漂移，
// 而漂移只会让后来的人不知道该信哪一处。
func assColour(hex string) (string, error) {
	h := strings.TrimSpace(hex)
	if !strings.HasPrefix(h, "#") {
		return "", fmt.Errorf("media: 字幕颜色必须以 # 开头（形如 #RRGGBB），实际 %q", hex)
	}
	h = h[1:]
	if len(h) != 6 {
		return "", fmt.Errorf("media: 字幕颜色必须是 #RRGGBB 形式，实际 %q", hex)
	}
	r, err1 := strconv.ParseUint(h[0:2], 16, 8)
	g, err2 := strconv.ParseUint(h[2:4], 16, 8)
	b, err3 := strconv.ParseUint(h[4:6], 16, 8)
	if err1 != nil || err2 != nil || err3 != nil {
		return "", fmt.Errorf("media: 字幕颜色含非法十六进制字符：%q", hex)
	}
	// 注意这里的顺序是 **B、G、R**：ASS 的 &H 颜色是 BGR 排列。
	return fmt.Sprintf("&H00%02X%02X%02X", b, g, r), nil
}

// clampInt 把 v 夹到 [lo, hi]。
func clampInt(v, lo, hi int) int {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}

// defaultLoudnessLUFS 是成片的目标响度。
//
// -16 LUFS 是网络视频平台的常见口径（比广播的 -23 LUFS 响、比流媒体音乐的
// -14 LUFS 轻）。选它的理由是：旁白清晰度优先，且不至于让配乐过响。
const defaultLoudnessLUFS = -16.0

// ---------------------------------------------------------------------------
// 调色（纯函数）
// ---------------------------------------------------------------------------

// gradeParams 是"强度拉满"时的调色参数。
//
// 用「1.0 = 不变」表示乘法型参数（对比度、饱和度）、用「0 = 不变」表示加法型
// （亮度），这样按强度插值只需要一套公式，不必为每种参数写特例。
//
// 色温用 `colortemperature`（K），6500K 为中性。**不要用 colorbalance**：
// 实测这台 ffmpeg 上它对画面完全没有影响 —— 拿纯灰图试过 `bm=0.5`（拉满的
// 蓝色中间调）、`pl=1`、以及先转 `gbrp` 再调，UAVG 一律纹丝不动（恒为 128），
// 而换 `colortemperature=9000` 立刻变成 136。第一版就是照文档写的 colorbalance，
// 滤镜串看着完全正确、命令也不报错，成片却和原片一模一样 ——
// 单测把它抓了出来，否则这会是一个"用户选了冷色调但没有任何变化"的静默缺陷。
type gradeParams struct {
	contrast   float64
	brightness float64
	saturation float64
	// temperature 是目标色温（K）。0 表示不动色温。
	temperature float64
	curves      string
}

// neutralTemperature 是色温的中性点（D65 日光）。
const neutralTemperature = 6500.0

// gradeTable 是内置调色方案。键名即对外暴露的 `grade.name`。
//
// 数值刻意**保守**：成片的主要任务是"把内容讲清楚"，调色只该起到统一观感的作用。
// 重口味滤镜在单张截图上好看，连起来看十几分钟会累。
var gradeTable = map[string]gradeParams{
	// 暖：色温下调 + 略提饱和，适合"生活化/亲切"的段落。
	"warm": {temperature: 5200, contrast: 1.04, saturation: 1.05},
	// 冷：色温上调，是科技类内容的默认气质。
	"cool": {temperature: 8200, contrast: 1.04, saturation: 1.02},
	// 高对比：压暗部、提亮部，让画面"立"起来（不动色温）。
	"high_contrast": {contrast: 1.20, brightness: -0.02, saturation: 1.05},
	// 胶片感：S 形曲线 + 降饱和 + 轻微偏暖。
	"film": {temperature: 5800, contrast: 1.08, saturation: 0.90, curves: "strong_contrast"},
}

// GradeNames 返回全部可用的调色方案名（含 none），供校验与文档使用。
//
// 排序输出：调用方可能直接拿它去拼用户可见的提示文案，
// 顺序不稳定会让同一条错误信息在不同进程里长得不一样。
func GradeNames() []string {
	names := make([]string, 0, len(gradeTable)+1)
	names = append(names, "none")
	for k := range gradeTable {
		names = append(names, k)
	}
	sort.Strings(names)
	return names
}

// PlanGrade 把调色方案翻译成 ffmpeg 视频滤镜串；不需要调色时返回空串。
//
// **未登记的方案名一律报错，不静默降级。** 静默降级在这里特别危险：
// 用户明明选了"胶片感"，成片却是原色，而没有任何地方提示过 ——
// 与批处理里"配错了就当作没配"是同一类问题。
//
// strength 的语义：0 与 1 都表示"完整效果"。想要关掉请用 `none`，
// 这样"忘了填"（JSON 零值）与"明确关闭"就不会混为一谈。
func PlanGrade(name string, strength float64) (string, error) {
	n := strings.ToLower(strings.TrimSpace(name))
	if n == "" || n == "none" {
		return "", nil
	}
	p, ok := gradeTable[n]
	if !ok {
		return "", fmt.Errorf("media: 未知的调色方案 %q；可用：%s",
			name, strings.Join(GradeNames(), ", "))
	}
	if strength <= 0 || strength > 1 {
		strength = 1
	}

	// 在「不变」与「完整效果」之间按强度插值。
	contrast := 1 + (p.contrast-1)*strength
	saturation := 1 + (p.saturation-1)*strength
	brightness := p.brightness * strength
	// 色温朝中性点插值：强度 0.5 时只走一半色偏。
	temperature := neutralTemperature
	if p.temperature > 0 {
		temperature = neutralTemperature + (p.temperature-neutralTemperature)*strength
	}

	var filters []string
	if p.curves != "" {
		filters = append(filters, "curves=preset="+p.curves)
	}
	if !near(temperature, neutralTemperature) {
		filters = append(filters, fmt.Sprintf("colortemperature=temperature=%.0f", temperature))
	}
	if !nearZero(contrast-1) || !nearZero(brightness) || !nearZero(saturation-1) {
		filters = append(filters, fmt.Sprintf("eq=contrast=%.4f:brightness=%.4f:saturation=%.4f",
			contrast, brightness, saturation))
	}
	return strings.Join(filters, ","), nil
}

// near 用固定容差比较两个浮点数（用于"其实没变"的判断）。
func near(a, b float64) bool { return nearZero(a - b) }

// nearZero 用固定容差判断"这个参数其实没变"。
//
// 不能直接比 0：强度插值会产生 1e-17 这种残渣，它会让滤镜串里多出一个
// 数值上毫无作用的参数，让"有没有效果"变得更难读。
func nearZero(v float64) bool { return v > -1e-6 && v < 1e-6 }

// PlanVideoChain 拼出后期处理要用的完整视频滤镜串。
//
// 顺序是刻意的，三处都不能调换：
//
//		调色 → 字幕 → 淡入淡出
//
//	  - 字幕放在调色**之后**：否则白色字幕会被一起染色，暖色调下字幕发黄，
//	    看起来像没擦干净的污渍；
//	  - 淡入淡出放在**最后**：否则片尾画面淡出了、字幕还亮着，像坏了。
//
// subtitleFilterName 是交给 `subtitles=` 的**字面值**（相对文件名或已转义路径），
// 由调用方决定 —— 见 Runner.runDir 里关于滤镜参数转义的说明。
func PlanVideoChain(opts PostOptions, durationSec float64, subtitleFilterName string) (string, error) {
	grade, err := PlanGrade(opts.Grade.Name, opts.Grade.Strength)
	if err != nil {
		return "", err
	}

	var parts []string
	if grade != "" {
		parts = append(parts, grade)
	}
	if subtitleFilterName != "" {
		style, serr := PlanSubtitleStyle(opts.SubtitleStyle, opts.FrameHeight)
		if serr != nil {
			return "", serr
		}
		// force_style 的值里含逗号，而逗号在滤镜图里是**滤镜分隔符** ——
		// 不用单引号把值包起来，整条滤镜链会被从中间劈成两半，
		// ffmpeg 只会报一句含糊的 "Invalid argument"。
		parts = append(parts, fmt.Sprintf("subtitles=%s:force_style='%s'", subtitleFilterName, style))
	}
	if opts.Fade.InSec > 0 {
		parts = append(parts, fmt.Sprintf("fade=t=in:st=0:d=%.3f", opts.Fade.InSec))
	}
	if opts.Fade.OutSec > 0 {
		start := durationSec - opts.Fade.OutSec
		if start < 0 {
			start = 0
		}
		parts = append(parts, fmt.Sprintf("fade=t=out:st=%.3f:d=%.3f", start, opts.Fade.OutSec))
	}
	// **必须把像素格式压回 yuv420p**，这是整条链的最后一步、也是不能省的一步。
	//
	// 踩过的坑很贵：`eq` / `curves` / `colortemperature` 在 RGB 空间工作，
	// `subtitles`(libass) 也会改像素格式，于是走完这条链就是 yuv444p ——
	// 而 libx264 会据此自动选 **High 4:4:4 Predictive**，那是专业中间格式，
	// QQ 影音 / 微信 / 手机 / 电视盒子**一律播不了**（VLC 软解能播，所以在
	// 开发机上完全看不出来）。
	//
	// 也就是说：**只要开了调色或烧录字幕，成片就会变成社交软件播不了的格式。**
	// 表现是"我这儿能看，发出去别人打不开"，而根因离表象非常远。
	// Normalize 那条链末尾有同样的 `format=yuv420p`，这里是补上漏掉的一处。
	if len(parts) > 0 {
		parts = append(parts, "format=yuv420p")
	}
	return strings.Join(parts, ","), nil
}

// NeedsPostProcess 报告这次是否真的需要跑一遍后期编码。
//
// 抽成独立函数是为了让"不请求效果就绝不多编码一次"变成**可断言**的性质：
// 调用方据此跳过整条后期链路，测试据此做反向对照。
func NeedsPostProcess(opts PostOptions) bool {
	if opts.BurnSubtitlePath != "" {
		return true
	}
	if opts.Fade.InSec > 0 || opts.Fade.OutSec > 0 {
		return true
	}
	n := strings.ToLower(strings.TrimSpace(opts.Grade.Name))
	return n != "" && n != "none"
}

// PostProcess 一次性应用调色 / 烧录字幕 / 淡入淡出。
//
// 三者合并成**同一次编码**是刻意的：它们都要求重编码，分开做会把一遍变三遍，
// 每一遍都再损一次画质。
func (r *Runner) PostProcess(ctx context.Context, in, out string, opts PostOptions, durationSec float64) error {
	if !NeedsPostProcess(opts) {
		return fmt.Errorf("media: 没有需要应用的后期效果（调用方应先问 NeedsPostProcess）")
	}
	if _, err := os.Stat(in); err != nil {
		return fmt.Errorf("media: 待后期处理的视频不存在 %s: %w", in, err)
	}

	// 烧录字幕时把工作目录设成字幕所在目录，滤镜参数里只留文件名 ——
	// 这样 Windows 的 `C:` 就不会落进滤镜图（理由见 runDir 的注释）。
	dir, subName := "", ""
	if opts.BurnSubtitlePath != "" {
		if _, err := os.Stat(opts.BurnSubtitlePath); err != nil {
			return fmt.Errorf("media: 待烧录的字幕不存在 %s: %w", opts.BurnSubtitlePath, err)
		}
		dir = filepath.Dir(opts.BurnSubtitlePath)
		subName = filepath.Base(opts.BurnSubtitlePath)
	}

	chain, err := PlanVideoChain(opts, durationSec, subName)
	if err != nil {
		return err
	}

	return r.runDir(ctx, dir,
		"-hide_banner", "-nostdin", "-y",
		"-i", in,
		"-map", "0:v:0", "-map", "0:a?",
		"-vf", chain,
		"-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
		// 显式写死像素格式，与滤镜链末尾的 format=yuv420p **双保险**。
		//
		// 少了它（或少了链尾那道 format），libx264 会跟着输入的 yuv444p 走，
		// 编出 High 4:4:4 Predictive —— 那是 QQ / 微信 / 手机都播不了的格式。
		// 两个都写上是因为它们防的是不同的东西：链尾的 format 保证送进编码器的
		// 是 420，这里的 -pix_fmt 保证编码器不会自作主张再改。
		"-pix_fmt", "yuv420p",
		// 音轨原样带走：这一层只动画面，重编码音频既无收益也多一次损失。
		"-c:a", "copy",
		// 与 Normalize 保持同一套色彩标记，否则调色后的成片与片段观感会不一致。
		"-color_range", "tv",
		"-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
		"-movflags", "+faststart",
		out,
	)
}

// ---------------------------------------------------------------------------
// 配乐
// ---------------------------------------------------------------------------

// audioFormatFilter 把任意输入统一成混音需要的格式。
//
// 必须显式统一：sidechaincompress 与 amix 都要求两路输入的采样率与声道布局一致，
// 否则 ffmpeg 直接报错，而错误信息只说"格式不匹配"，不会告诉你是哪一路。
const audioFormatFilter = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"

// duck* 是闪避（sidechain compression）的参数。
//
// 语义是：以旁白为触发信号，旁白一响就把配乐压下去，停下来的几百毫秒内慢慢恢复。
// 参数取值的依据：
//   - threshold 取 0.05（约 -26 dBFS）—— 比正常旁白电平低不少，
//     保证"有人说话"能被稳定检出，而不是偶尔漏掉轻声句；
//   - ratio 8 让压缩足够明显，否则配乐盖住旁白的问题依旧存在；
//   - release 350ms 是"不突兀"与"不拖沓"之间的折中：太快会让配乐在句间
//     一起一伏像呼吸机，太慢则下一句开始时配乐还没让开。
const (
	duckThreshold = 0.05
	duckRatio     = 8.0
	duckAttackMs  = 20.0
	duckReleaseMs = 350.0
)

// bgmReferenceLUFS 是配乐的**基准响度**。
//
// 为什么需要基准而不是直接调 `volume`：上传的音乐文件响度千差万别
// （有的整轨在 -3 LUFS、有的在 -25 LUFS）。同一个 `volume_db` 作用在它们身上
// 得到的结果完全不同 —— 用户会觉得"这个音量滑块时灵时不灵"。
//
// 先归一到基准，`volume_db` 才成为**相对基准的偏移**，滑块才有可预期性。
//
// 取 -20 而不是对白目标的 -16：配乐是**垫底**的，本就该比对白低一截。
// 这同时也是"默认 BGM 太响"的根因 —— 原来纯 BGM 的成片会被最后那道
// `loudnorm=I=-16` 直接拽到对白响度，而它本该比旁白轻。
const bgmReferenceLUFS = -20.0

// BuildBgmTrack 把背景音乐配成**与成片等长**的一条音轨。
//
// 三种情形都要处理，且都不能报错收场：
//   - 配乐比成片短 -> 循环（Loop=true 时用 `-stream_loop`）；
//   - 配乐比成片长 -> 裁到成片长度；
//   - 两端 -> 淡入淡出，避免开头"啪"地切入和结尾突然截断。
//
// 另外会**先**把配乐归一到 bgmReferenceLUFS，**再**施加 `volume_db` 偏移 ——
// 顺序不能反：先加偏移再归一，偏移量会被归一化抹掉，滑块就完全失效了。
func (r *Runner) BuildBgmTrack(ctx context.Context, in, out string, targetSec float64, spec BgmSpec) error {
	if strings.TrimSpace(in) == "" {
		return fmt.Errorf("media: 背景音乐路径为空")
	}
	if _, err := os.Stat(in); err != nil {
		return fmt.Errorf("media: 背景音乐文件不存在 %s: %w", in, err)
	}
	if targetSec <= 0 {
		return fmt.Errorf("media: 背景音乐目标时长必须为正，实际 %.3f", targetSec)
	}

	fadeIn, fadeOut := fitFades(spec.FadeInSec, spec.FadeOutSec, targetSec)

	filters := []string{
		// 先归一到基准响度，让后面的 volume 偏移可预期（见 bgmReferenceLUFS）。
		fmt.Sprintf("loudnorm=I=%.1f:TP=-1.5:LRA=11", bgmReferenceLUFS),
		fmt.Sprintf("atrim=0:%.3f", targetSec),
		// 裁切后必须重排时间戳，否则被裁掉的前段会让后续滤镜看到错位的时间轴。
		"asetpts=N/SR/TB",
	}
	if !nearZero(spec.VolumeDB) {
		filters = append(filters, fmt.Sprintf("volume=%.2fdB", spec.VolumeDB))
	}
	if fadeIn > 0 {
		filters = append(filters, fmt.Sprintf("afade=t=in:st=0:d=%.3f", fadeIn))
	}
	if fadeOut > 0 {
		st := targetSec - fadeOut
		if st < 0 {
			st = 0
		}
		filters = append(filters, fmt.Sprintf("afade=t=out:st=%.3f:d=%.3f", st, fadeOut))
	}

	args := []string{"-hide_banner", "-nostdin", "-y"}
	if spec.Loop {
		// `-stream_loop -1` 是**输入选项**，必须放在 -i 之前 ——
		// 放到后面会被当成未知的输出选项。
		args = append(args, "-stream_loop", "-1")
	}
	args = append(args,
		"-i", in,
		"-vn",
		"-af", strings.Join(filters, ","),
		// `-t` 是硬保险：`-stream_loop -1` 遇到解析异常的容器时可能读不完，
		// 没有它，一条配乐能撑出几个小时的文件。
		"-t", fmt.Sprintf("%.3f", targetSec),
		"-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
		"-movflags", "+faststart",
		out,
	)
	return r.run(ctx, args...)
}

// fitFades 把淡入淡出收紧到目标时长之内。
//
// 不收紧的后果很隐蔽：目标 3 秒、配乐设了 2 秒淡入 + 2 秒淡出时，两段 fade
// 在时间轴上重叠，ffmpeg 不报错，但实际听感是"一直在淡入的曲子"。
func fitFades(fadeIn, fadeOut, target float64) (float64, float64) {
	if fadeIn < 0 {
		fadeIn = 0
	}
	if fadeOut < 0 {
		fadeOut = 0
	}
	if fadeIn+fadeOut <= target {
		return fadeIn, fadeOut
	}
	// 按比例缩到刚好铺满，保证两段不重叠。
	scale := target / (fadeIn + fadeOut)
	return fadeIn * scale, fadeOut * scale
}

// MixSoundtrack 把旁白与配乐混成最终音轨。
//
// 三种输入组合都要支持，因为它们在真实使用中都会出现：
//   - 都有 -> 闪避后混音（这是配乐功能的**主要价值**：配乐不抢旁白）；
//   - 只有旁白 -> 只做响度归一化；
//   - 只有配乐 -> 只做响度归一化（用户关了 TTS 但仍想要背景音乐）。
//
// 末尾统一过 loudnorm：没有它，"配乐开大一点"就会让成片整体响度飘忽，
// 而响度不一致是观众最容易察觉、又最难说清的一类问题。
func (r *Runner) MixSoundtrack(ctx context.Context, voicePath, bgmPath, out string, loudnessLUFS float64) error {
	if voicePath == "" && bgmPath == "" {
		return fmt.Errorf("media: 没有可混合的音轨（旁白与配乐都为空）")
	}
	if loudnessLUFS == 0 {
		loudnessLUFS = defaultLoudnessLUFS
	}
	loudnorm := fmt.Sprintf("loudnorm=I=%.1f:TP=-1.5:LRA=11", loudnessLUFS)

	var args []string
	args = append(args, "-hide_banner", "-nostdin", "-y")

	var graph string
	switch {
	case voicePath != "" && bgmPath != "":
		args = append(args, "-i", voicePath, "-i", bgmPath)
		graph = fmt.Sprintf(
			"[0:a]%s[voice];[1:a]%s[bgm];"+
				// 以旁白为侧链触发闪避：配乐让路，旁白始终清楚。
				"[bgm][voice]sidechaincompress=threshold=%.3f:ratio=%.1f:"+
				"attack=%.1f:release=%.1f:makeup=1[ducked];"+
				// duration=first + normalize=0：长度跟随旁白（它已被配成与成片等长），
				// 且**不做自动归一化** —— amix 默认会把两路各减半，
				// 那会把辛苦调好的配乐音量又莫名其妙压下去。
				"[voice][ducked]amix=inputs=2:duration=first:normalize=0[mixed];"+
				"[mixed]%s[out]",
			audioFormatFilter, audioFormatFilter,
			duckThreshold, duckRatio, duckAttackMs, duckReleaseMs,
			loudnorm,
		)
	case voicePath != "":
		args = append(args, "-i", voicePath)
		graph = fmt.Sprintf("[0:a]%s[voice];[voice]%s[out]", audioFormatFilter, loudnorm)
	default:
		// 只有配乐：**刻意不做**最后那道响度归一。
		//
		// 它是**对白口径**（-16 LUFS）。把纯音乐拽到那个响度，正是"背景音乐太响"
		// 的直接原因 —— 用户听到的是一整轨按人声响度播的音乐。
		// 配乐在 BuildBgmTrack 里已经归一到 bgmReferenceLUFS(-20)，
		// 这里再归一一次不但多余，还会把它重新拉响。
		args = append(args, "-i", bgmPath)
		graph = fmt.Sprintf("[0:a]%s[out]", audioFormatFilter)
	}

	args = append(args,
		"-filter_complex", graph,
		"-map", "[out]",
		"-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
		"-movflags", "+faststart",
		out,
	)
	return r.run(ctx, args...)
}
