// Package media 封装 ffmpeg / ffprobe，负责分镜片段的校验、归一化与合成。
//
// 设计原则：
//  1. **一切外部进程都必须绑定 context**，取消时立刻 Kill，杜绝僵尸进程与磁盘写满。
//  2. **合成前必须校验**：不同渲染引擎（Manim / headless 浏览器 / 代码动画）产出的
//     片段在分辨率、帧率、像素格式、时基上几乎一定不一致，直接 concat 会得到
//     花屏或音画不同步。因此先 probe，不一致先归一化。
//  3. 并发由调用方（composer）控制，本包只提供**线程安全**的原语。
package media

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/itJinYu/SciDirector/backend/internal/config"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// ErrFFmpegNotFound 表示可执行文件不存在，属于部署配置错误，应当快速失败。
var ErrFFmpegNotFound = errors.New("media: 找不到 ffmpeg/ffprobe 可执行文件")

// ProbeResult 是 ffprobe 的关键结论，只保留合成决策需要的字段。
type ProbeResult struct {
	Path        string
	DurationSec float64
	Width       int
	Height      int
	FPS         float64
	VideoCodec  string
	PixFmt      string
	HasAudio    bool
	AudioCodec  string
	BitRate     int64
	// ColorRange 是色彩范围（tv/limited 或 pc/full）。
	// 它是「同一部片子里这段发灰、那段正常」的首要原因，因此必须可被观测 ——
	// 不可观测的约定等于没有约定。
	ColorRange string
	// ColorSpace 是色彩空间标记（如 bt709）。未标注时为空串。
	ColorSpace string
	// HasSubtitle 表示文件内含字幕流。
	HasSubtitle bool
	// SubtitleCodec 是字幕流编码（如 mov_text）。
	SubtitleCodec string
}

// ffprobeOutput 对应 `ffprobe -print_format json -show_format -show_streams` 的结构。
// 只声明用得到的字段，避免被 ffprobe 的庞大输出绑架。
type ffprobeOutput struct {
	Format struct {
		Duration string `json:"duration"`
		BitRate  string `json:"bit_rate"`
	} `json:"format"`
	Streams []struct {
		CodecType    string `json:"codec_type"`
		CodecName    string `json:"codec_name"`
		Width        int    `json:"width"`
		Height       int    `json:"height"`
		PixFmt       string `json:"pix_fmt"`
		AvgFrameRate string `json:"avg_frame_rate"`
		RFrameRate   string `json:"r_frame_rate"`
		Duration     string `json:"duration"`
		ColorRange   string `json:"color_range"`
		ColorSpace   string `json:"color_space"`
	} `json:"streams"`
}

// procWaitDelay 是「进程已退出、但它的后代仍占着 stdout/stderr 管道」时的
// 兜底等待上限。
//
// 这是 Go 的一个经典陷阱：exec 在 Stderr 是 io.Writer（非 *os.File）时会建一条
// os.Pipe，并起一个 goroutine 把内容拷走。如果子进程派生的**孙进程**继承了这条
// 管道的写端，那么即使子进程已经死了，管道写端仍未关闭，拷贝 goroutine 不会退出，
// cmd.Wait() 就会**永久阻塞** —— 表现为「任务取消失了，但 goroutine 永远卡住」。
// 设了 WaitDelay 之后，超时即强制关闭管道并返回，进程与调用方都能脱身。
const procWaitDelay = 5 * time.Second

// defaultTransitionSec 是未显式配置时的转场时长。
const defaultTransitionSec = 0.4

// Runner 是无状态的 ffmpeg / ffprobe 调用器，可安全地被多个 goroutine 共用。
type Runner struct {
	ffmpegBin  string
	ffprobeBin string
	// sem 限制**整个进程内**同时在跑的外部媒体进程数。
	//
	// 这是防 OOM 的**全局**闸门，理由有二：
	//  1. 单个 ffmpeg 会吃满多核，无节制并发会让「并发」退化为互相抢占，
	//     总耗时反而更长；
	//  2. 每个 ffmpeg 处理 1080p 时峰值内存可达数百 MB，几十个并发足以打爆内存。
	//
	// 关键在于它是 Runner 的字段，而 Runner 在 worker 进程中是**单例**：
	// 因此无论有多少个任务、多少个调用点（归一化 / 合成 / 抽帧 / 转码），
	// 加起来都不会超过这个上限。若把它做成「每个任务一个」，上限就会随
	// 并发任务数成倍放大，等于没有限制。
	sem *Semaphore
	// cmdTimeout 是单条外部命令的硬超时。
	// 没有它，一个卡死的 ffmpeg 会永久占住一个槽位；占满 MaxParallel 个之后
	// 整条流水线彻底停摆 —— 这比进程崩溃更难恢复，因为没有错误可报。
	cmdTimeout time.Duration
	// defaultSpec 是配置决定的归一化目标规格，供编排层直接使用。
	defaultSpec NormalizeSpec
	// transition 是配置决定的转场方案（是否真的启用由 PlanTransitions 判定）。
	transition TransitionSpec
}

// DefaultNormalizeSpec 返回配置决定的归一化目标规格。
func (r *Runner) DefaultNormalizeSpec() NormalizeSpec { return r.defaultSpec }

// Transition 返回配置决定的转场方案。
func (r *Runner) Transition() TransitionSpec { return r.transition }

// NewRunner 依据配置构造 Runner。
//
// 返回 error 而不是静默降级：转场名拼错属于配置错误，
// 必须在进程启动期就报出来，而不是等到第一部成片出来才发现「转场没了」。
func NewRunner(cfg config.MediaConfig) (*Runner, error) {
	parallel := cfg.MaxParallel
	if parallel < 1 {
		parallel = 1
	}
	cmdTimeout := cfg.CommandTimeout
	if cmdTimeout <= 0 {
		cmdTimeout = 10 * time.Minute
	}

	transitionType, err := ParseTransitionType(cfg.Transition)
	if err != nil {
		return nil, err
	}
	transitionDur := cfg.TransitionDurationSec
	if transitionDur <= 0 {
		transitionDur = defaultTransitionSec
	}

	fps := cfg.FPS
	if fps < 1 {
		fps = 30
	}

	return &Runner{
		ffmpegBin:  cfg.FFmpegBin,
		ffprobeBin: cfg.FFprobeBin,
		sem:        NewSemaphore(parallel),
		cmdTimeout: cmdTimeout,
		defaultSpec: NormalizeSpec{
			Width:  cfg.Width,
			Height: cfg.Height,
			FPS:    fps,
			Color: ColorProfile{
				Saturation: cfg.ColorSaturation,
				Contrast:   cfg.ColorContrast,
				Gamma:      cfg.ColorGamma,
				Brightness: cfg.ColorBrightness,
			},
		},
		transition: TransitionSpec{Type: transitionType, DurationSec: transitionDur},
	}, nil
}

// MaxParallel 暴露全局并发上限，供启动日志与自检使用。
func (r *Runner) MaxParallel() int { return cap(r.sem.ch) }

// newCmd 构造一条**生命周期受控**的外部命令。
//
// 三重保护，缺一不可：
//  1. 独立进程组（setupProcAttr）—— 保证取消时能杀整棵树而不是只杀直接子进程；
//  2. 自定义 Cancel（killTree）—— 覆盖 exec 默认的「只杀直接子进程」行为；
//  3. WaitDelay —— 兜住「进程已死但管道被后代占用」导致的 Wait 永久阻塞。
//
// 返回的 cancel 必须被调用，否则命令级的超时定时器会泄漏。
func (r *Runner) newCmd(ctx context.Context, bin string, args ...string) (*exec.Cmd, context.CancelFunc) {
	if r.cmdTimeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, r.cmdTimeout)
		cmd := exec.CommandContext(ctx, bin, args...)
		applyProcessControl(cmd)
		return cmd, cancel
	}
	cmd := exec.CommandContext(ctx, bin, args...)
	applyProcessControl(cmd)
	return cmd, func() {}
}

// applyProcessControl 给命令装上进程组与超时兜底。
func applyProcessControl(cmd *exec.Cmd) {
	setupProcAttr(cmd)
	cmd.WaitDelay = procWaitDelay
	// 注意：一旦自定义 Cancel，就等于接管了「取消时怎么杀」这件事。
	cmd.Cancel = func() error {
		return killTree(cmd.Process)
	}
}

// Verify 检查 ffmpeg / ffprobe 是否可用。
// 在进程启动时调用，让「镜像里没装 ffmpeg」这类错误在启动期就暴露。
func (r *Runner) Verify(ctx context.Context) error {
	for _, bin := range []string{r.ffmpegBin, r.ffprobeBin} {
		path, err := exec.LookPath(bin)
		if err != nil {
			return fmt.Errorf("%w: %s", ErrFFmpegNotFound, bin)
		}
		checkCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
		cmd := exec.CommandContext(checkCtx, path, "-version")
		var out bytes.Buffer
		cmd.Stdout = &out
		runErr := cmd.Run()
		cancel()
		if runErr != nil {
			return fmt.Errorf("media: 执行 %s -version 失败: %w", bin, runErr)
		}
	}
	return nil
}

// acquire 获取一个全局进程槽位，尊重 ctx 取消。
func (r *Runner) acquire(ctx context.Context) (func(), error) {
	return r.sem.Acquire(ctx)
}

// run 执行一次 ffmpeg 命令。
//
// 返回的错误中会附带 ffmpeg 的 stderr 尾部 —— 这是排查渲染/合成问题最有价值的信息，
// 必须保留而不是丢弃。
func (r *Runner) run(ctx context.Context, args ...string) error {
	release, err := r.acquire(ctx)
	if err != nil {
		return err
	}
	defer release()

	cmd, cancel := r.newCmd(ctx, r.ffmpegBin, args...)
	defer cancel()

	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	// stdout 通常是进度信息，丢弃以免缓冲区被撑爆（-progress 场景另行处理）。
	cmd.Stdout = nil

	start := time.Now()
	runErr := cmd.Run()
	if runErr != nil {
		if ctx.Err() != nil {
			// 上下文取消导致的失败：明确返回 ctx 错误，不要伪装成 ffmpeg 失败，
			// 否则上层会把「用户取消」误判为「渲染出错」而触发无意义重试。
			return fmt.Errorf("media: ffmpeg 被取消: %w", ctx.Err())
		}
		// WaitDelay 触发：进程大概率已死，是后代进程占着管道。
		// 这不属于「ffmpeg 执行失败」，而是清理不彻底，必须能一眼认出来。
		if errors.Is(runErr, exec.ErrWaitDelay) {
			return fmt.Errorf("media: ffmpeg 超时后未能彻底退出（后代进程占用管道，已强制放弃等待，耗时 %s）: %w",
				time.Since(start).Round(time.Millisecond), runErr)
		}
		return fmt.Errorf("media: ffmpeg 执行失败（%s，耗时 %s）: %w\nstderr: %s",
			strings.Join(args, " "), time.Since(start).Round(time.Millisecond), runErr, tail(stderr.String(), 2000))
	}
	return nil
}

// Probe 读取媒体文件的关键参数。
//
// ffprobe 同样计入全局并发闸门：它虽然比转码轻，但对大文件仍要读容器索引，
// 且**同样可能卡死**（损坏的文件、网络挂载的路径）。让它绕过闸门，
// 就等于给「一个卡死的探测占满所有资源」留了后门。
func (r *Runner) Probe(ctx context.Context, path string) (*ProbeResult, error) {
	if _, err := os.Stat(path); err != nil {
		return nil, fmt.Errorf("media: 待探测文件不存在 %s: %w", path, err)
	}

	release, err := r.acquire(ctx)
	if err != nil {
		return nil, err
	}
	defer release()

	cmd, cancel := r.newCmd(ctx, r.ffprobeBin,
		"-v", "error",
		"-print_format", "json",
		"-show_format",
		"-show_streams",
		path,
	)
	defer cancel()

	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		if ctx.Err() != nil {
			return nil, fmt.Errorf("media: ffprobe 被取消: %w", ctx.Err())
		}
		return nil, fmt.Errorf("media: ffprobe 失败 %s: %w\nstderr: %s", path, err, tail(stderr.String(), 1000))
	}

	var raw ffprobeOutput
	if err := json.Unmarshal(stdout.Bytes(), &raw); err != nil {
		return nil, fmt.Errorf("media: 解析 ffprobe 输出失败 %s: %w", path, err)
	}

	res := &ProbeResult{Path: path}
	res.DurationSec = parseFloat(raw.Format.Duration)
	res.BitRate = int64(parseFloat(raw.Format.BitRate))

	for _, s := range raw.Streams {
		switch s.CodecType {
		case "video":
			// 一个文件可能有多个视频流（封面图等），取第一个真实视频流。
			if res.Width == 0 {
				res.Width = s.Width
				res.Height = s.Height
				res.VideoCodec = s.CodecName
				res.PixFmt = s.PixFmt
				res.ColorRange = s.ColorRange
				res.ColorSpace = s.ColorSpace
				// avg_frame_rate 有时是 "0/0"，此时回退到 r_frame_rate。
				res.FPS = parseRational(s.AvgFrameRate)
				if res.FPS <= 0 {
					res.FPS = parseRational(s.RFrameRate)
				}
				// 容器级 duration 可能缺失（例如某些 TS 片段），用流级兜底。
				if res.DurationSec <= 0 {
					res.DurationSec = parseFloat(s.Duration)
				}
			}
		case "audio":
			res.HasAudio = true
			res.AudioCodec = s.CodecName
		case "subtitle":
			res.HasSubtitle = true
			res.SubtitleCodec = s.CodecName
		}
	}

	if res.Width == 0 {
		return nil, fmt.Errorf("media: %s 中未找到有效的视频流（可能渲染产物损坏或为空）", path)
	}
	if res.DurationSec <= 0 {
		// 宁可报错也不要放一个时长为 0 的片段进合成流程，那会毁掉整条音画同步。
		return nil, fmt.Errorf("media: %s 时长为 0 或无法解析，判定为无效产物", path)
	}
	return res, nil
}

// Normalize 把任意片段归一化到统一规格，使 concat / xfade 可靠。
//
// 归一化**不只是**统一分辨率与帧率，还包括：
//   - 统一像素格式（yuv420p）；
//   - 统一色彩范围（转换到 limited/tv 并显式打标）；
//   - 统一色彩空间标记（bt709）；
//   - 无音轨时补静音轨，保证所有片段结构一致。
//
// 每一项都是 concat -c copy 与 xfade 能工作的必要条件。只统一分辨率
// 而放着色彩范围不管，成片里就会出现「这段发灰、那段正常」的割裂感 ——
// 而这在单个片段上看不出来，只有拼在一起才暴露。
//
// 使用 scale + pad 而非强制拉伸，避免改变画面宽高比。
func (r *Runner) Normalize(ctx context.Context, in, out string, spec NormalizeSpec) error {
	return r.run(ctx,
		"-hide_banner", "-nostdin", "-y",
		"-i", in,
		// 无音轨时补一条静音轨，保证所有片段结构一致（后续 mux / acrossfade 才不会错位）。
		"-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
		"-map", "0:v:0", "-map", "1:a:0",
		"-vf", spec.normalizeVideoFilter(),
		"-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
		"-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
		// 显式写出色彩标记。仅靠滤镜转换是不够的：不打标时播放器只能猜，
		// 而不同的播放器猜法不同，同一部成片在两个平台上观感不一致。
		"-color_range", "tv",
		"-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
		"-shortest",
		"-movflags", "+faststart",
		out,
	)
}

// Concat 用 concat demuxer 合并已归一化的片段。
//
// 前置条件：所有输入片段的编码参数必须一致（由 Normalize 保证）。
// 这是最快且不损失画质的合并方式；若参数不一致，结果会花屏或时长错乱。
func (r *Runner) Concat(ctx context.Context, listFile, out string) error {
	return r.run(ctx,
		"-hide_banner", "-nostdin", "-y",
		"-f", "concat", "-safe", "0",
		"-i", listFile,
		"-c", "copy",
		"-movflags", "+faststart",
		out,
	)
}

// WriteConcatList 生成 concat demuxer 所需的清单文件。
//
// 两个转义细节都不能省，且**顺序不能反**：
//
//  1. Windows 路径的反斜杠在 concat 清单里必须写成正斜杠，否则 ffmpeg 会把它
//     当成转义字符；
//  2. 路径中的单引号必须写成 '\”，否则会提前闭合 file '...' 的引号。
//
// 必须**先** ToSlash **再**转义引号。反过来写是一个真实踩过的坑：
// ToSlash 会把引号转义刚引入的反斜杠一并替换掉，`'\”` 被破坏成 `'/”`，
// 清单语法失效 —— 而 ffmpeg 对此不一定报错，可能只产出一部时长错乱的成片。
func WriteConcatList(listPath string, files []string) error {
	var sb strings.Builder
	for _, f := range files {
		abs, err := filepath.Abs(f)
		if err != nil {
			return fmt.Errorf("media: 解析绝对路径失败 %s: %w", f, err)
		}
		escaped := filepath.ToSlash(abs)
		escaped = strings.ReplaceAll(escaped, "'", `'\''`)
		sb.WriteString("file '")
		sb.WriteString(escaped)
		sb.WriteString("'\n")
	}
	if err := os.MkdirAll(filepath.Dir(listPath), 0o755); err != nil {
		return fmt.Errorf("media: 创建清单目录失败: %w", err)
	}
	if err := os.WriteFile(listPath, []byte(sb.String()), 0o644); err != nil {
		return fmt.Errorf("media: 写入 concat 清单失败: %w", err)
	}
	return nil
}

// MuxFinal 把成片视频、总音轨与字幕合成为最终交付物。
//
// 参数语义：
//   - audioPath 为空表示视频已自带音轨，直接复制；
//   - subtitlePath 为空表示不挂字幕；否则以 mov_text 封装为软字幕（可开关，不破坏画面）。
//
// 输入下标必须**动态跟踪**，不能写死。字幕输入的下标取决于中间是否插了音轨：
// 有 TTS 音轨时字幕是 2 号输入，没有时就是 1 号输入。
// 早期实现把 `-map 2:s:0` 写死了，于是在「视频自带音轨、不额外传 audioPath」
// 这条**最常见**的路径上，字幕下标永远指向不存在的流 ——
// 表现为软字幕静默封不进去，而画面和音轨都正常，很难怀疑到映射上。
func (r *Runner) MuxFinal(ctx context.Context, videoPath, audioPath, subtitlePath, out string) error {
	var args []string
	args = append(args, "-hide_banner", "-nostdin", "-y")

	nextIdx := 0
	args = append(args, "-i", videoPath)
	videoIdx := nextIdx
	nextIdx++

	audioIdx := -1
	if audioPath != "" {
		args = append(args, "-i", audioPath)
		audioIdx = nextIdx
		nextIdx++
	}

	subIdx := -1
	if subtitlePath != "" {
		args = append(args, "-i", subtitlePath)
		subIdx = nextIdx
		nextIdx++
	}

	args = append(args, "-map", fmt.Sprintf("%d:v:0", videoIdx))
	if audioIdx >= 0 {
		args = append(args, "-map", fmt.Sprintf("%d:a:0", audioIdx))
	} else {
		// `?` 让缺失音轨不至于让整条命令失败。
		args = append(args, "-map", fmt.Sprintf("%d:a?", videoIdx))
	}
	if subIdx >= 0 {
		args = append(args, "-map", fmt.Sprintf("%d:s:0", subIdx), "-c:s", "mov_text")
	}

	args = append(args,
		"-c:v", "copy",
		"-c:a", "aac", "-b:a", "192k",
	)

	// -shortest 只在**混入外部音轨**时才加，因为它要解决的问题正是
	// 「外部音轨比画面长，导致末尾一段黑屏」。
	//
	// 视频自带音轨时它没有任何作用，却会造成一个极隐蔽的破坏：
	// 当某条字幕恰好结束于视频末尾（我们的字幕对齐逻辑**总是**这样），
	// -shortest 会把该字幕包的时长压成 0 —— 字幕流依然存在、ffprobe 也能探到，
	// 但内容是空的，抽回 SRT 得到 0 字节文件。
	// 这类「流在、内容没了」的失败比直接报错难发现得多。
	if audioIdx >= 0 {
		args = append(args, "-shortest")
	}

	args = append(args, "-movflags", "+faststart", out)
	return r.run(ctx, args...)
}

// ExtractFrames 从视频中均匀抽取 count 帧 PNG，供 VLM 审查。
//
// 抽帧策略（与 Critic Agent 的 rubric 对齐）：
//   - 均匀采样覆盖整体节奏；
//   - **额外包含首帧与末帧**：入场/收尾的字幕截断、元素溢出最容易出现在这两处，
//     均匀采样常常恰好漏掉它们。
func (r *Runner) ExtractFrames(ctx context.Context, videoPath, outDir string, count int, durationSec float64) ([]string, error) {
	if count < 1 {
		count = 3
	}
	if err := os.MkdirAll(outDir, 0o755); err != nil {
		return nil, fmt.Errorf("media: 创建抽帧目录失败: %w", err)
	}
	if durationSec <= 0 {
		// 交给调用方 probe；这里只做兜底，避免除零。
		durationSec = 1
	}

	// 采样点：把时间轴切成 count 段，取每段中点；再补首末帧。
	times := make([]float64, 0, count+2)
	for i := 0; i < count; i++ {
		times = append(times, durationSec*(float64(i)+0.5)/float64(count))
	}
	times = append(times, 0.0, maxFloat(durationSec-0.05, 0))

	frames := make([]string, 0, len(times))
	for i, t := range times {
		out := filepath.Join(outDir, fmt.Sprintf("frame_%02d.png", i))
		// -ss 放在 -i 之前是关键帧快速定位，速度远快于解码后再 seek。
		err := r.run(ctx,
			"-hide_banner", "-nostdin", "-y",
			"-ss", strconv.FormatFloat(t, 'f', 3, 64),
			"-i", videoPath,
			"-frames:v", "1",
			// 缩放到 1024 宽：足够 VLM 判断字号与排版，又能显著降低上传成本。
			"-vf", "scale=1024:-2",
			out,
		)
		if err != nil {
			// 抽帧失败不应让整个审查流程失败：跳过该帧继续。
			// 但如果一帧都没抽到，调用方会拿到空切片并据此降级到人工。
			continue
		}
		frames = append(frames, out)
	}
	if len(frames) == 0 {
		return nil, fmt.Errorf("media: 从 %s 抽帧全部失败", videoPath)
	}
	return frames, nil
}

// ColorProfile 描述**统一调色**参数。
//
// 为什么需要它：本项目的片段来自三类差异极大的渲染器 ——
// Manim（矢量、有限色彩范围）、无头浏览器逐帧截图（RGB 全范围）、
// ffmpeg lavfi 合成。它们在成片里并排出现时会产生明显的「质感割裂」。
//
// 割裂的**首要原因是色彩范围不匹配**，而不是艺术风格差异：
// 全范围内容被当成有限范围播放会显得发灰、对比度偏低。
// 因此统一规格必须显式声明并转换色彩范围（见 Normalize），
// 这里的参数只是在此基础上提供一层**全片一致**的微调能力。
//
// 零值表示该项不调整 —— 注意 eq 滤镜的默认值是 saturation/contrast/gamma = 1.0，
// 把未配置的项写成 0 会让画面直接变黑，所以必须逐项判断而不是无脑拼参数。
type ColorProfile struct {
	Saturation float64 // 1.0 为原始饱和度
	Contrast   float64 // 1.0 为原始对比度
	Gamma      float64 // 1.0 为原始伽马
	Brightness float64 // 0.0 为原始亮度
}

// filterExpr 把非零项编译成 eq 滤镜表达式；全为零时返回空串。
func (c ColorProfile) filterExpr() string {
	var parts []string
	if c.Saturation > 0 {
		parts = append(parts, fmt.Sprintf("saturation=%.4f", c.Saturation))
	}
	if c.Contrast > 0 {
		parts = append(parts, fmt.Sprintf("contrast=%.4f", c.Contrast))
	}
	if c.Gamma > 0 {
		parts = append(parts, fmt.Sprintf("gamma=%.4f", c.Gamma))
	}
	if c.Brightness != 0 {
		parts = append(parts, fmt.Sprintf("brightness=%.4f", c.Brightness))
	}
	if len(parts) == 0 {
		return ""
	}
	return "eq=" + strings.Join(parts, ":")
}

// NormalizeSpec 描述归一化的目标规格。
type NormalizeSpec struct {
	Width  int
	Height int
	FPS    int
	Color  ColorProfile
}

// normalizeVideoFilter 构造归一化的视频滤镜链。
//
// 顺序有讲究：先缩放与补边（几何），再统一帧率（时间），再做调色（像素），
// 最后落到 yuv420p。把 format 放最后是必须的 —— eq 滤镜在 RGB 下工作，
// 若先转成 yuv420p 再做色彩调整，会引入一次多余的有损往返。
//
// in_range=auto:out_range=limited 是「统一质感」的关键一步：
// 标了全范围的输入会被真正转换到有限范围，而不是被错误地当成有限范围播出去。
// 输入未标注范围时 auto 按有限范围处理，等于不做多余转换 —— 两种情况都正确。
func (s NormalizeSpec) normalizeVideoFilter() string {
	vf := fmt.Sprintf(
		"scale=%d:%d:force_original_aspect_ratio=decrease:in_range=auto:out_range=limited,"+
			"pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black,"+
			"fps=%d",
		s.Width, s.Height, s.Width, s.Height, s.FPS,
	)
	if eq := s.Color.filterExpr(); eq != "" {
		vf += "," + eq
	}
	return vf + ",format=yuv420p"
}

// ConcatWithTransition 用 xfade 把片段**带转场地**拼接起来。
//
// 与 Concat 的本质区别：xfade 必须解码再编码，因此**无法使用 -c copy**。
// 这是转场的真实代价，也是默认走硬切路径的原因 —— 调用方应当先问
// PlanTransitions 是否真的启用了转场，再决定走哪条路。
//
// 前置条件（由 Normalize 保证）：所有输入的分辨率、帧率、像素格式、
// 色彩范围、时基完全一致，且都带音轨。任一条不满足都会导致花屏、
// 转场位置漂移或滤镜图直接报错。
func (r *Runner) ConcatWithTransition(ctx context.Context, inputs []string, out string, plan TransitionPlan) error {
	if !plan.Enabled {
		return fmt.Errorf("media: 转场方案未启用（%s），不应走 xfade 路径", plan.Reason)
	}
	filter, err := BuildXFadeFilter(plan, len(inputs))
	if err != nil {
		return err
	}

	args := []string{"-hide_banner", "-nostdin", "-y"}
	for _, in := range inputs {
		args = append(args, "-i", in)
	}
	args = append(args,
		"-filter_complex", filter,
		"-map", "[vout]", "-map", "[aout]",
		"-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
		"-pix_fmt", "yuv420p",
		"-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
		// 与 Normalize 使用完全相同的色彩标记，保证成片内部一致。
		"-color_range", "tv",
		"-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
		"-movflags", "+faststart",
		out,
	)
	return r.run(ctx, args...)
}

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

// parseFloat 宽容解析 ffprobe 的字符串数值，失败返回 0。
func parseFloat(s string) float64 {
	if s == "" || s == "N/A" {
		return 0
	}
	f, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return 0
	}
	return f
}

// parseRational 解析形如 "30000/1001" 的有理数帧率。
func parseRational(s string) float64 {
	if s == "" || s == "N/A" {
		return 0
	}
	parts := strings.SplitN(s, "/", 2)
	if len(parts) != 2 {
		return parseFloat(s)
	}
	num, err1 := strconv.ParseFloat(parts[0], 64)
	den, err2 := strconv.ParseFloat(parts[1], 64)
	if err1 != nil || err2 != nil || den == 0 {
		return 0
	}
	return num / den
}

// tail 返回字符串末尾至多 n 个字节（用于日志中保留 stderr 的关键部分）。
func tail(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return "…" + s[len(s)-n:]
}

func maxFloat(a, b float64) float64 {
	if a > b {
		return a
	}
	return b
}

// Semaphore 是一个可复用的并发闸门，供上层编排任意需要限流的批量操作。
type Semaphore struct {
	ch chan struct{}
}

// NewSemaphore 创建容量为 n 的闸门（n < 1 时按 1 处理）。
func NewSemaphore(n int) *Semaphore {
	if n < 1 {
		n = 1
	}
	return &Semaphore{ch: make(chan struct{}, n)}
}

// Acquire 获取槽位，返回释放函数。
func (s *Semaphore) Acquire(ctx context.Context) (func(), error) {
	select {
	case s.ch <- struct{}{}:
		var once sync.Once
		return func() { once.Do(func() { <-s.ch }) }, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

// ---------------------------------------------------------------------------
// 整片配音轨
// ---------------------------------------------------------------------------

// NarrationPart 是一个镜头在配音轨里的输入。
type NarrationPart struct {
	// AudioPath 是该镜头的配音文件（TTS 产出）。为空表示该镜头没有配音。
	AudioPath string
	// TargetSec 是该镜头在成片时间轴上的时长。音频会被**补齐或截断**到它，
	// 这样拼出来的整轨与画面严格等长。
	TargetSec float64
}

// narrationSampleRate / narrationChannels 是整轨的统一音频规格。
//
// 必须统一：concat 与 acrossfade 都要求各段的采样率与声道数一致，
// 不一致时 ffmpeg 往往不报错、只是产出错位或爆音的成片。
const (
	narrationSampleRate = 48000
	narrationChannels   = 2
)

// BuildNarrationTrack 把逐镜头配音拼成**一条与成片等长的整轨**。
//
// 为什么是「逐镜头合成 → 各自对齐到镜头时长 → concat」，而不是把全片文本
// 一次性丢给 TTS：
//  1. 字幕需要**每个镜头的真实配音时长**才能排准（见 PlanCuesWithNarration）；
//     逐镜头合成天然产出这个信息，整段合成则拿不到；
//  2. 镜头被人工打回重做时，只需重做它那一段音频，不必整片重合成 ——
//     与局部重渲染是同一个降本思路。
//
// 没有配音的镜头补**等长静音**，而不是跳过：
// 跳过会让整轨比画面短，其后每一个镜头的声音都会整体前移 ——
// 「某段没声音」只是小瑕疵，「全片音画错位」是废片。
func (r *Runner) BuildNarrationTrack(ctx context.Context, parts []NarrationPart, out string) error {
	if len(parts) == 0 {
		return fmt.Errorf("media: 配音轨至少需要一个片段")
	}

	workDir := filepath.Dir(out)
	if err := os.MkdirAll(workDir, 0o755); err != nil {
		return fmt.Errorf("media: 创建配音轨目录失败: %w", err)
	}

	segPaths := make([]string, 0, len(parts))
	for i, part := range parts {
		if part.TargetSec <= 0 {
			return fmt.Errorf("media: 第 %d 段配音的目标时长必须为正，实际 %.3f", i, part.TargetSec)
		}
		seg := filepath.Join(workDir, fmt.Sprintf("narration_%03d.m4a", i))

		var err error
		if strings.TrimSpace(part.AudioPath) == "" {
			err = r.buildSilentSegment(ctx, part.TargetSec, seg)
		} else {
			err = r.buildNarrationSegment(ctx, part.AudioPath, part.TargetSec, seg)
		}
		if err != nil {
			return fmt.Errorf("media: 生成第 %d 段配音失败: %w", i, err)
		}
		segPaths = append(segPaths, seg)
	}

	listPath := filepath.Join(workDir, "narration.txt")
	if err := WriteConcatList(listPath, segPaths); err != nil {
		return err
	}
	// 音频 concat 用 -c copy 风险高（各段编码器延迟可能不同），统一重编码，
	// 代价是几秒钟的 CPU，换来的是「不会莫名其妙少几十毫秒」。
	if err := r.run(ctx,
		"-hide_banner", "-nostdin", "-y",
		"-f", "concat", "-safe", "0",
		"-i", listPath,
		"-c:a", "aac", "-ar", fmt.Sprintf("%d", narrationSampleRate),
		"-ac", fmt.Sprintf("%d", narrationChannels),
		out,
	); err != nil {
		return fmt.Errorf("media: 拼接配音轨失败: %w", err)
	}
	return nil
}

// buildNarrationSegment 把一段配音归一化到目标时长。
//
// `apad` 先把音频无限补静音，再由 `-t` 截到目标时长：
// 音频偏短 → 补静音；音频偏长 → 截断。两者都让这一段与画面严格等长。
func (r *Runner) buildNarrationSegment(ctx context.Context, in string, targetSec float64, out string) error {
	if err := r.run(ctx,
		"-hide_banner", "-nostdin", "-y",
		"-i", in,
		"-af", "apad",
		"-t", fmt.Sprintf("%.3f", targetSec),
		"-ar", fmt.Sprintf("%d", narrationSampleRate),
		"-ac", fmt.Sprintf("%d", narrationChannels),
		"-c:a", "aac",
		out,
	); err != nil {
		return fmt.Errorf("media: 归一化配音段失败 %s: %w", in, err)
	}
	return nil
}

// buildSilentSegment 生成一段等长静音，用于没有配音的镜头。
func (r *Runner) buildSilentSegment(ctx context.Context, targetSec float64, out string) error {
	if err := r.run(ctx,
		"-hide_banner", "-nostdin", "-y",
		"-f", "lavfi",
		"-i", fmt.Sprintf("anullsrc=r=%d:cl=stereo", narrationSampleRate),
		"-t", fmt.Sprintf("%.3f", targetSec),
		"-c:a", "aac",
		out,
	); err != nil {
		return fmt.Errorf("media: 生成静音段失败: %w", err)
	}
	return nil
}

// ProbeAudio 探测一个**纯音频**文件的时长（配音轨用），单位秒。
//
// 为什么不复用 Probe：Probe 是「渲染产物校验器」，它**要求存在视频流** ——
// 一个只有音频的文件在它眼里就是无效产物。这条校验对镜头片段是对的，
// 但对配音文件不适用，也不该为了配音去放宽它。
// 因此配音时长走这条独立的、只读 format.duration 的路径。
func (r *Runner) ProbeAudio(ctx context.Context, path string) (float64, error) {
	if _, err := os.Stat(path); err != nil {
		return 0, fmt.Errorf("media: 配音文件不存在 %s: %w", path, err)
	}

	release, err := r.acquire(ctx)
	if err != nil {
		return 0, err
	}
	defer release()

	cmd, cancel := r.newCmd(ctx, r.ffprobeBin,
		"-v", "error",
		"-show_entries", "format=duration",
		"-of", "default=nw=1:nk=1",
		path,
	)
	defer cancel()

	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		if ctx.Err() != nil {
			return 0, fmt.Errorf("media: ffprobe 被取消: %w", ctx.Err())
		}
		return 0, fmt.Errorf("media: 探测配音时长失败 %s: %w\nstderr: %s",
			path, err, tail(stderr.String(), 500))
	}

	sec, perr := strconv.ParseFloat(strings.TrimSpace(stdout.String()), 64)
	if perr != nil || sec <= 0 {
		return 0, fmt.Errorf("media: 配音时长非法 %s: %q", path, strings.TrimSpace(stdout.String()))
	}
	return sec, nil
}
