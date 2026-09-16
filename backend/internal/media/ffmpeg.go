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
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/itJinYu/SciDirector/backend/internal/config"
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
}

// NewRunner 依据配置构造 Runner。
func NewRunner(cfg config.MediaConfig) *Runner {
	parallel := cfg.MaxParallel
	if parallel < 1 {
		parallel = 1
	}
	cmdTimeout := cfg.CommandTimeout
	if cmdTimeout <= 0 {
		cmdTimeout = 10 * time.Minute
	}
	return &Runner{
		ffmpegBin:  cfg.FFmpegBin,
		ffprobeBin: cfg.FFprobeBin,
		sem:        NewSemaphore(parallel),
		cmdTimeout: cmdTimeout,
	}
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

// Normalize 把任意片段归一化到统一规格，使 concat 可靠。
//
// 归一化目标由渲染配置决定（默认 1920x1080@30fps，yuv420p）。
// 使用 scale + pad 而非强制拉伸，避免改变画面宽高比。
func (r *Runner) Normalize(ctx context.Context, in, out string, width, height, fps int) error {
	vf := fmt.Sprintf(
		"scale=%d:%d:force_original_aspect_ratio=decrease,pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black,fps=%d,format=yuv420p",
		width, height, width, height, fps,
	)
	return r.run(ctx,
		"-hide_banner", "-nostdin", "-y",
		"-i", in,
		// 无音轨时补一条静音轨，保证所有片段结构一致（后续 mux 才不会错位）。
		"-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
		"-map", "0:v:0", "-map", "1:a:0",
		"-vf", vf,
		"-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
		"-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
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
func (r *Runner) MuxFinal(ctx context.Context, videoPath, audioPath, subtitlePath, out string) error {
	args := []string{"-hide_banner", "-nostdin", "-y", "-i", videoPath}

	hasAudio := audioPath != ""
	if hasAudio {
		args = append(args, "-i", audioPath)
	}
	hasSub := subtitlePath != ""
	if hasSub {
		args = append(args, "-i", subtitlePath)
	}

	args = append(args, "-map", "0:v:0")
	if hasAudio {
		args = append(args, "-map", "1:a:0")
	} else {
		args = append(args, "-map", "0:a?")
	}
	if hasSub {
		args = append(args, "-map", "2:s:0", "-c:s", "mov_text")
	}

	args = append(args,
		"-c:v", "copy",
		"-c:a", "aac", "-b:a", "192k",
		// shortest 保证音视频任一路先结束时整体结束，避免出现黑屏尾巴。
		"-shortest",
		"-movflags", "+faststart",
		out,
	)
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
