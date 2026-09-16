package media

import (
	"context"
	"errors"
	"fmt"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/itJinYu/SciDirector/backend/internal/config"
)

// ---------------------------------------------------------------------------
// 测试辅助
// ---------------------------------------------------------------------------

// requireFFmpeg 在缺少 ffmpeg/ffprobe 时跳过测试。
//
// 跳过而不是失败：CI 的轻量镜像与开发机的能力不同，把「工具没装」
// 和「代码写错了」混为一谈，会让真正的问题被淹没在噪声里。
func requireFFmpeg(t *testing.T) {
	t.Helper()
	for _, bin := range []string{"ffmpeg", "ffprobe"} {
		if _, err := exec.LookPath(bin); err != nil {
			t.Skipf("未找到 %s，跳过真实媒体集成测试: %v", bin, err)
		}
	}
}

// newTestRunner 构造一个并发上限可控的 Runner。
func newTestRunner(t *testing.T, maxParallel int) *Runner {
	t.Helper()
	return newTestRunnerWith(t, config.MediaConfig{
		FFmpegBin:      "ffmpeg",
		FFprobeBin:     "ffprobe",
		MaxParallel:    maxParallel,
		CommandTimeout: 2 * time.Minute,
		Width:          320,
		Height:         240,
		FPS:            15,
	})
}

// newTestRunnerWith 允许覆写媒体配置（转场、调色等）。
func newTestRunnerWith(t *testing.T, cfg config.MediaConfig) *Runner {
	t.Helper()
	r, err := NewRunner(cfg)
	if err != nil {
		t.Fatalf("构造 Runner 失败: %v", err)
	}
	return r
}

// testSpec 是测试统一使用的归一化规格。
func testSpec() NormalizeSpec {
	return NormalizeSpec{Width: 320, Height: 240, FPS: 15}
}

// makeClip 用 lavfi 生成一段测试视频，避免测试依赖任何外部素材。
func makeClip(t *testing.T, r *Runner, out string, size string, rate int, seconds int) {
	t.Helper()
	err := r.run(context.Background(),
		"-hide_banner", "-nostdin", "-y",
		"-f", "lavfi",
		"-i", fmt.Sprintf("testsrc=duration=%d:size=%s:rate=%d", seconds, size, rate),
		"-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
		out,
	)
	if err != nil {
		t.Fatalf("生成测试片段 %s 失败: %v", out, err)
	}
}

// ---------------------------------------------------------------------------
// 并发上限：本次改动的核心验收
// ---------------------------------------------------------------------------

// TestRunnerBoundsRealFFmpegProcesses 用**真实 ffmpeg 进程**验证全局并发闸门。
//
// 观测手段是直接读信号量当前占用量 len(r.sem.ch) —— 它恰好等于「此刻正在跑的
// 外部进程数」。这比去数系统进程表更可靠，也不会因为环境里别的 ffmpeg 而误判。
func TestRunnerBoundsRealFFmpegProcesses(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}

	const limit = 2
	const jobs = 6

	r := newTestRunner(t, limit)
	if got := r.MaxParallel(); got != limit {
		t.Fatalf("MaxParallel() = %d，期望 %d", got, limit)
	}

	// 边跑边采样，记录观测到的最大并发进程数。
	stop := make(chan struct{})
	sampled := make(chan int64, 1)
	go func() {
		var peak int64
		tick := time.NewTicker(time.Millisecond)
		defer tick.Stop()
		for {
			select {
			case <-stop:
				sampled <- peak
				return
			case <-tick.C:
				if n := int64(len(r.sem.ch)); n > peak {
					peak = n
				}
			}
		}
	}()

	// 用 veryslow 预设 + 丢弃输出，把单次编码拉长到足以重叠。
	pool := NewPool(limit)
	err := pool.Run(context.Background(), jobs, func(ctx context.Context, i int) error {
		return r.run(ctx,
			"-hide_banner", "-nostdin", "-y",
			"-f", "lavfi", "-i", "testsrc=duration=1:size=640x480:rate=30",
			"-c:v", "libx264", "-preset", "veryslow",
			"-f", "null", "-",
		)
	})
	close(stop)
	peak := <-sampled

	if err != nil {
		t.Fatalf("并发编码失败: %v", err)
	}
	if peak > limit {
		t.Fatalf("观测到 %d 个 ffmpeg 同时运行，超过全局上限 %d —— OOM 防线失效", peak, limit)
	}
	if peak < limit {
		t.Fatalf("观测到的并发峰值只有 %d，期望达到 %d（并发未真正发生，测试没有覆盖到目标场景）", peak, limit)
	}
}

// TestRunnerGlobalGateIsSharedAcrossPools 证明「多个任务共用同一个全局闸门」。
//
// 这是防 OOM 的关键性质：如果每个任务各自持有一个信号量，
// 那么 N 个并发任务的上限就是 N × MaxParallel，等于没有限制。
func TestRunnerGlobalGateIsSharedAcrossPools(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}

	const limit = 2
	r := newTestRunner(t, limit)

	stop := make(chan struct{})
	sampled := make(chan int64, 1)
	go func() {
		var peak int64
		tick := time.NewTicker(time.Millisecond)
		defer tick.Stop()
		for {
			select {
			case <-stop:
				sampled <- peak
				return
			case <-tick.C:
				if n := int64(len(r.sem.ch)); n > peak {
					peak = n
				}
			}
		}
	}()

	// 两个「任务」各自跑自己的 Pool：它们**不共享** Pool，但共享 Runner 的闸门。
	task := func() error {
		p := NewPool(limit)
		return p.Run(context.Background(), 3, func(ctx context.Context, i int) error {
			return r.run(ctx,
				"-hide_banner", "-nostdin", "-y",
				"-f", "lavfi", "-i", "testsrc=duration=1:size=640x480:rate=30",
				"-c:v", "libx264", "-preset", "veryslow",
				"-f", "null", "-",
			)
		})
	}

	done := make(chan error, 2)
	go func() { done <- task() }()
	go func() { done <- task() }()
	for i := 0; i < 2; i++ {
		if err := <-done; err != nil {
			close(stop)
			<-sampled
			t.Fatalf("并发任务失败: %v", err)
		}
	}
	close(stop)
	peak := <-sampled

	if peak > limit {
		t.Fatalf("两个任务并存时观测到 %d 个 ffmpeg，超过全局上限 %d —— 闸门不是全局的", peak, limit)
	}
}

// TestRunnerCancelKillsFFmpegPromptly 覆盖验收项 B3：
// 取消任务时 ffmpeg 必须被真正杀掉，而不是继续把 CPU 跑到自然结束。
func TestRunnerCancelKillsFFmpegPromptly(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}

	r := newTestRunner(t, 1)

	// 让 ffmpeg 干一段「正常情况下要跑很久」的活：120 秒素材 + veryslow 编码。
	// 若取消失效，这个调用会持续几十秒；生效则应在毫秒级返回。
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(400 * time.Millisecond)
		cancel()
	}()

	start := time.Now()
	err := r.run(ctx,
		"-hide_banner", "-nostdin", "-y",
		"-f", "lavfi", "-i", "testsrc=duration=120:size=1280x720:rate=30",
		"-c:v", "libx264", "-preset", "veryslow",
		"-f", "null", "-",
	)
	elapsed := time.Since(start)
	cancel()

	if err == nil {
		t.Fatal("取消后 ffmpeg 不应报告成功")
	}
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("取消应返回 context.Canceled（而不是伪装成 ffmpeg 失败），实际: %v", err)
	}
	// 400ms 后取消 + 5s WaitDelay 上限，10s 是很宽松的界。
	if elapsed > 10*time.Second {
		t.Fatalf("取消后耗时 %s，说明 ffmpeg 没有被及时杀掉", elapsed.Round(time.Millisecond))
	}
	// 槽位必须被归还：否则取消几次之后并发容量就被永久吃掉。
	waitFor(t, 2*time.Second, func() bool { return len(r.sem.ch) == 0 },
		"取消后信号量槽位未归还")
}

// ---------------------------------------------------------------------------
// 合成链路：验收项 B2
// ---------------------------------------------------------------------------

// TestNormalizeAndConcatDifferentSpecs 覆盖验收项 B2：
// 分辨率/帧率/像素格式都不一致的片段，归一化后必须能无损 concat，
// 且成片的规格正确、时长等于各片段之和。
func TestNormalizeAndConcatDifferentSpecs(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}

	r := newTestRunner(t, 2)
	dir := t.TempDir()

	// 两个刻意做得不一致的片段：尺寸、帧率都不同。
	clipA := filepath.Join(dir, "a.mp4")
	clipB := filepath.Join(dir, "b.mp4")
	makeClip(t, r, clipA, "320x240", 15, 2)
	makeClip(t, r, clipB, "640x360", 25, 2)

	const (
		wantW   = 320
		wantH   = 240
		wantFPS = 15
	)

	ctx := context.Background()
	normPaths := make([]string, 2)
	for i, in := range []string{clipA, clipB} {
		out := filepath.Join(dir, fmt.Sprintf("norm_%d.mp4", i))
		if err := r.Normalize(ctx, in, out, NormalizeSpec{Width: wantW, Height: wantH, FPS: wantFPS}); err != nil {
			t.Fatalf("归一化 %s 失败: %v", in, err)
		}
		normPaths[i] = out
	}

	// 归一化后的参数必须完全一致 —— 这正是 concat -c copy 能无损拼接的前提。
	for i, p := range normPaths {
		probe, err := r.Probe(ctx, p)
		if err != nil {
			t.Fatalf("探测归一化产物 %d 失败: %v", i, err)
		}
		if probe.Width != wantW || probe.Height != wantH {
			t.Fatalf("产物 %d 尺寸 %dx%d，期望 %dx%d", i, probe.Width, probe.Height, wantW, wantH)
		}
		if math.Abs(probe.FPS-wantFPS) > 0.5 {
			t.Fatalf("产物 %d 帧率 %.2f，期望 %d", i, probe.FPS, wantFPS)
		}
		if probe.PixFmt != "yuv420p" {
			t.Fatalf("产物 %d 像素格式 %q，期望 yuv420p", i, probe.PixFmt)
		}
		if !probe.HasAudio {
			t.Fatalf("产物 %d 缺少音轨 —— 无音轨片段会让后续 mux 错位", i)
		}
	}

	list := filepath.Join(dir, "concat.txt")
	if err := WriteConcatList(list, normPaths); err != nil {
		t.Fatalf("写 concat 清单失败: %v", err)
	}
	merged := filepath.Join(dir, "merged.mp4")
	if err := r.Concat(ctx, list, merged); err != nil {
		t.Fatalf("concat 失败: %v", err)
	}

	probe, err := r.Probe(ctx, merged)
	if err != nil {
		t.Fatalf("探测成片失败: %v", err)
	}
	if probe.Width != wantW || probe.Height != wantH {
		t.Fatalf("成片尺寸 %dx%d，期望 %dx%d", probe.Width, probe.Height, wantW, wantH)
	}
	// 两段各 2 秒。允许一定误差：编码器对首尾帧的处理会有毫秒级差异。
	if probe.DurationSec < 3.5 || probe.DurationSec > 4.5 {
		t.Fatalf("成片时长 %.2fs，期望约 4s（两段各 2s）—— 片段可能被截断或拼接错位", probe.DurationSec)
	}
}

// TestConcatHandlesApostropheInPath 是上面转义逻辑的**端到端**证明：
// 让 ffmpeg 真的去读一份含单引号的 concat 清单。
//
// 只断言字符串形态是不够的 —— ffmpeg 解析失败时未必报错，也可能只产出一部
// 时长不对的成片。所以这里直接验证成片时长，让「转义写错了」无处可藏。
func TestConcatHandlesApostropheInPath(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}

	r := newTestRunner(t, 2)
	dir := t.TempDir()

	// 刻意把单引号放进文件名：这是 Windows/macOS 上完全合法的文件名，
	// 也确实会出现在用户上传的素材路径里。
	quoted := filepath.Join(dir, "it's clip.mp4")
	plain := filepath.Join(dir, "plain.mp4")
	makeClip(t, r, quoted, "320x240", 15, 2)
	makeClip(t, r, plain, "320x240", 15, 2)

	ctx := context.Background()
	normQuoted := filepath.Join(dir, "norm_q.mp4")
	normPlain := filepath.Join(dir, "norm_p.mp4")
	if err := r.Normalize(ctx, quoted, normQuoted, testSpec()); err != nil {
		t.Fatalf("归一化含单引号路径失败: %v", err)
	}
	if err := r.Normalize(ctx, plain, normPlain, testSpec()); err != nil {
		t.Fatalf("归一化普通路径失败: %v", err)
	}

	list := filepath.Join(dir, "concat.txt")
	if err := WriteConcatList(list, []string{normQuoted, normPlain}); err != nil {
		t.Fatalf("写清单失败: %v", err)
	}
	merged := filepath.Join(dir, "merged.mp4")
	if err := r.Concat(ctx, list, merged); err != nil {
		t.Fatalf("ffmpeg 无法解析含单引号的 concat 清单（转义有误）: %v", err)
	}

	probe, err := r.Probe(ctx, merged)
	if err != nil {
		t.Fatalf("探测成片失败: %v", err)
	}
	// 两段各 2 秒。若清单只被解析出第一段（引号提前闭合的典型症状），
	// 时长会明显偏短。
	if probe.DurationSec < 3.5 {
		t.Fatalf("成片时长仅 %.2fs，期望约 4s —— 清单可能只被解析出部分片段", probe.DurationSec)
	}
}

// ---------------------------------------------------------------------------
// 转场与统一调色：阶段三②的验收
// ---------------------------------------------------------------------------

// TestNormalizeUnifiesColorRangeAndSpace 是「统一调色」的验收项。
//
// 割裂的首要来源是色彩范围不匹配：Manim 的矢量输出是有限范围，
// 无头浏览器录制出来的是 RGB 全范围。两者拼在一部片子里，
// 全范围那段会被当成有限范围播出去，显得发灰、对比度偏低。
//
// 这里刻意构造一个**显式标注为全范围**的输入，验证它被归一化后
// 与另一个普通输入的色彩标记完全一致。
func TestNormalizeUnifiesColorRangeAndSpace(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}

	r := newTestRunner(t, 2)
	dir := t.TempDir()
	ctx := context.Background()

	// 片段 A：普通视频（ffmpeg 默认按有限范围编码）。
	plain := filepath.Join(dir, "plain.mp4")
	makeClip(t, r, plain, "320x240", 15, 2)

	// 片段 B：显式标记为全范围（pc）的内容 —— 模拟浏览器录制。
	fullRange := filepath.Join(dir, "full.mp4")
	if err := r.run(ctx,
		"-hide_banner", "-nostdin", "-y",
		"-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=15",
		"-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
		"-color_range", "pc", "-colorspace", "bt709",
		fullRange,
	); err != nil {
		t.Fatalf("生成全范围测试片段失败: %v", err)
	}

	// 确认这个输入确实是全范围，否则整个测试的前提不成立。
	pre, err := r.Probe(ctx, fullRange)
	if err != nil {
		t.Fatalf("探测全范围片段失败: %v", err)
	}
	if pre.ColorRange != "pc" {
		t.Skipf("本机 ffmpeg 未能把输入标为 pc（实际 %q），跳过色彩范围一致性测试", pre.ColorRange)
	}

	normPlain := filepath.Join(dir, "n_plain.mp4")
	normFull := filepath.Join(dir, "n_full.mp4")
	if err := r.Normalize(ctx, plain, normPlain, testSpec()); err != nil {
		t.Fatalf("归一化普通片段失败: %v", err)
	}
	if err := r.Normalize(ctx, fullRange, normFull, testSpec()); err != nil {
		t.Fatalf("归一化全范围片段失败: %v", err)
	}

	a, err := r.Probe(ctx, normPlain)
	if err != nil {
		t.Fatalf("探测普通片段产物失败: %v", err)
	}
	b, err := r.Probe(ctx, normFull)
	if err != nil {
		t.Fatalf("探测全范围片段产物失败: %v", err)
	}

	// 核心断言：两个来源不同的片段，归一化后色彩标记必须**完全一致**。
	if a.ColorRange != "tv" {
		t.Errorf("普通片段归一化后 color_range = %q，期望 tv", a.ColorRange)
	}
	if b.ColorRange != "tv" {
		t.Errorf("全范围片段归一化后 color_range = %q，期望被转换并标记为 tv", b.ColorRange)
	}
	if a.ColorSpace != b.ColorSpace {
		t.Errorf("色彩空间标记不一致：%q vs %q —— 播放器会分别解释，观感割裂",
			a.ColorSpace, b.ColorSpace)
	}
	if a.PixFmt != "yuv420p" || b.PixFmt != "yuv420p" {
		t.Errorf("像素格式不一致：%q vs %q", a.PixFmt, b.PixFmt)
	}
}

// TestConcatWithTransitionRealMerge 是转场的端到端验收：
// 真实跑一次 xfade，验证成片时长等于 Σ时长 - (n-1)×T。
//
// 只断言「命令没报错」是不够的 —— offset 算错时 ffmpeg 多数情况下**不会报错**，
// 只会产出转场位置漂移的片子。所以必须用时长的算术关系来验证。
func TestConcatWithTransitionRealMerge(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}

	r := newTestRunner(t, 2)
	dir := t.TempDir()
	ctx := context.Background()

	// 三段各 2 秒。刻意用不同的输入尺寸，顺带验证归一化确实把规格拉平了。
	sizes := []string{"320x240", "640x360", "320x240"}
	inputs := make([]string, 0, len(sizes))
	durations := make([]float64, 0, len(sizes))

	for i, size := range sizes {
		raw := filepath.Join(dir, fmt.Sprintf("raw_%d.mp4", i))
		makeClip(t, r, raw, size, 15, 2)
		norm := filepath.Join(dir, fmt.Sprintf("norm_%d.mp4", i))
		if err := r.Normalize(ctx, raw, norm, testSpec()); err != nil {
			t.Fatalf("归一化 %d 失败: %v", i, err)
		}
		p, err := r.Probe(ctx, norm)
		if err != nil {
			t.Fatalf("探测 %d 失败: %v", i, err)
		}
		inputs = append(inputs, norm)
		durations = append(durations, p.DurationSec)
	}

	const wantTransition = 0.5
	plan := PlanTransitions(durations, TransitionSpec{Type: TransitionFade, DurationSec: wantTransition})
	if !plan.Enabled {
		t.Fatalf("应当启用转场：%s", plan.Reason)
	}
	if math.Abs(plan.Duration-wantTransition) > 1e-9 {
		t.Fatalf("统一转场时长 = %.3f，期望 %.3f（片段都够长，不该被压缩）", plan.Duration, wantTransition)
	}

	merged := filepath.Join(dir, "merged_xfade.mp4")
	if err := r.ConcatWithTransition(ctx, inputs, merged, plan); err != nil {
		t.Fatalf("转场合成失败: %v", err)
	}

	out, err := r.Probe(ctx, merged)
	if err != nil {
		t.Fatalf("探测成片失败: %v", err)
	}

	sum := 0.0
	for _, d := range durations {
		sum += d
	}
	wantOut := sum - float64(len(inputs)-1)*plan.Duration

	// 允许 0.35s 误差：编码器对首尾帧的处理、以及容器时长的取整都会带来偏差，
	// 但足以区分「正确交叠」与「完全没交叠」（后者会差 1.0s）或「offset 漂移」。
	if math.Abs(out.DurationSec-wantOut) > 0.35 {
		t.Fatalf("成片时长 %.3fs，期望约 %.3fs（Σ%.3f - %d×%.3f）—— 转场未按预期交叠",
			out.DurationSec, wantOut, sum, len(inputs)-1, plan.Duration)
	}
	// 成片必须比简单相加更短，这是「交叠而非插入」的直接证据。
	if out.DurationSec >= sum {
		t.Fatalf("成片时长 %.3fs 未短于片段之和 %.3fs —— 转场没有真正发生",
			out.DurationSec, sum)
	}
	if out.Width != 320 || out.Height != 240 {
		t.Fatalf("成片尺寸 %dx%d，期望 320x240", out.Width, out.Height)
	}
	if out.ColorRange != "tv" {
		t.Errorf("成片 color_range = %q，期望 tv", out.ColorRange)
	}
}

// TestConcatWithTransitionRejectsDisabledPlan 守住 API 边界：
// 方案未启用时不能悄悄走 xfade 路径。
func TestConcatWithTransitionRejectsDisabledPlan(t *testing.T) {
	requireFFmpeg(t)
	r := newTestRunner(t, 1)
	err := r.ConcatWithTransition(context.Background(),
		[]string{"a.mp4", "b.mp4"}, t.TempDir()+"/out.mp4",
		TransitionPlan{Enabled: false, Reason: "测试"})
	if err == nil {
		t.Fatal("未启用的转场方案应报错，而不是静默合成")
	}
}

// TestNewRunnerValidatesTransitionName 验证配置错误在构造期暴露。
func TestNewRunnerValidatesTransitionName(t *testing.T) {
	_, err := NewRunner(config.MediaConfig{
		FFmpegBin: "ffmpeg", FFprobeBin: "ffprobe",
		MaxParallel: 1, Transition: "fadee", // 拼错
		Width: 320, Height: 240, FPS: 15,
	})
	if err == nil {
		t.Fatal("非法转场名应在构造 Runner 时报错，而不是静默降级为硬切")
	}

	// 默认值应当是 fade，且时长落到默认值。
	r, err := NewRunner(config.MediaConfig{
		FFmpegBin: "ffmpeg", FFprobeBin: "ffprobe",
		MaxParallel: 1, Transition: "fade",
		Width: 320, Height: 240, FPS: 15,
	})
	if err != nil {
		t.Fatalf("构造失败: %v", err)
	}
	if r.Transition().Type != TransitionFade {
		t.Errorf("转场类型 = %q，期望 fade", r.Transition().Type)
	}
	if math.Abs(r.Transition().DurationSec-defaultTransitionSec) > 1e-9 {
		t.Errorf("未配置时长时应回落到默认 %.2f，实际 %.3f",
			defaultTransitionSec, r.Transition().DurationSec)
	}
}

// TestColorProfileFilterExpr 覆盖调色表达式的生成。
//
// 关键在「未配置的项必须省略」：eq 滤镜的默认值是 saturation/contrast/gamma = 1.0，
// 若把未配置项写成 0，画面会直接变黑。
func TestColorProfileFilterExpr(t *testing.T) {
	if got := (ColorProfile{}).filterExpr(); got != "" {
		t.Errorf("全零配置不应生成滤镜，实际 %q", got)
	}

	got := ColorProfile{Saturation: 1.1}.filterExpr()
	if !strings.Contains(got, "saturation=1.1000") {
		t.Errorf("应包含饱和度设置，实际 %q", got)
	}
	if strings.Contains(got, "contrast") || strings.Contains(got, "gamma") {
		t.Errorf("未配置的项绝不能被写入（会变成 0 导致画面全黑），实际 %q", got)
	}

	// 亮度为 0 是「不调整」，负值才是有意义配置。
	if got := (ColorProfile{Brightness: -0.05}).filterExpr(); !strings.Contains(got, "brightness=-0.0500") {
		t.Errorf("负亮度应被写入，实际 %q", got)
	}
	if got := (ColorProfile{Saturation: 1.1, Contrast: 1.2}).filterExpr(); !strings.HasPrefix(got, "eq=") {
		t.Errorf("应生成 eq 滤镜，实际 %q", got)
	}
}

// TestNormalizeVideoFilterOrdering 验证滤镜链的顺序：
// 几何 → 时间 → 调色 → format，且 format 必须在最后。
func TestNormalizeVideoFilterOrdering(t *testing.T) {
	spec := NormalizeSpec{Width: 640, Height: 360, FPS: 30, Color: ColorProfile{Saturation: 1.1}}
	vf := spec.normalizeVideoFilter()

	iScale := strings.Index(vf, "scale=")
	iFps := strings.Index(vf, "fps=")
	iEq := strings.Index(vf, "eq=")
	iFmt := strings.Index(vf, "format=yuv420p")

	if iScale < 0 || iFps < 0 || iEq < 0 || iFmt < 0 {
		t.Fatalf("滤镜链缺少必要环节: %s", vf)
	}
	if !(iScale < iFps && iFps < iEq && iEq < iFmt) {
		t.Fatalf("滤镜顺序应为 几何→时间→调色→format，实际: %s", vf)
	}
	if !strings.Contains(vf, "in_range=auto:out_range=limited") {
		t.Errorf("必须显式声明色彩范围转换，否则全范围输入会显示异常: %s", vf)
	}

	// 未配置调色时不应出现 eq。
	plain := NormalizeSpec{Width: 640, Height: 360, FPS: 30}.normalizeVideoFilter()
	if strings.Contains(plain, "eq=") {
		t.Errorf("未配置调色时不应生成 eq: %s", plain)
	}
}

// ---------------------------------------------------------------------------
// 字幕与软字幕封装：阶段三③的验收
// ---------------------------------------------------------------------------

// TestMuxFinalEmbedsSoftSubtitle 是软字幕封装的端到端验收。
//
// 「软字幕」的要点是它作为独立字幕流存在、可开关，而不是烧进画面。
// 因此判据必须是**探测到字幕流**，光看命令没报错是不够的 ——
// 字幕文件路径写错时 ffmpeg 可能报错，但流映射写错时它会静默产出无字幕成片。
func TestMuxFinalEmbedsSoftSubtitle(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}

	r := newTestRunner(t, 1)
	dir := t.TempDir()
	ctx := context.Background()

	clip := filepath.Join(dir, "clip.mp4")
	makeClip(t, r, clip, "320x240", 15, 3)
	norm := filepath.Join(dir, "norm.mp4")
	if err := r.Normalize(ctx, clip, norm, testSpec()); err != nil {
		t.Fatalf("归一化失败: %v", err)
	}

	srt := filepath.Join(dir, "final.srt")
	cues := []Cue{
		{Start: 0, End: 1.5, Text: "第一句字幕"},
		{Start: 1.5, End: 3.0, Text: "第二句字幕"},
	}
	if err := WriteSRT(srt, cues); err != nil {
		t.Fatalf("写字幕失败: %v", err)
	}

	out := filepath.Join(dir, "final.mp4")
	if err := r.MuxFinal(ctx, norm, "", srt, out); err != nil {
		t.Fatalf("封装软字幕失败: %v", err)
	}

	probe, err := r.Probe(ctx, out)
	if err != nil {
		t.Fatalf("探测成片失败: %v", err)
	}
	if !probe.HasSubtitle {
		t.Fatal("成片中没有字幕流 —— 软字幕未被封装进去")
	}
	if probe.SubtitleCodec != "mov_text" {
		t.Errorf("字幕编码 = %q，期望 mov_text（MP4 容器的软字幕格式）",
			probe.SubtitleCodec)
	}
	// 画面不能被破坏：尺寸与音轨都要保持。
	if probe.Width != 320 || probe.Height != 240 {
		t.Errorf("成片尺寸 %dx%d，期望 320x240", probe.Width, probe.Height)
	}
	if !probe.HasAudio {
		t.Error("成片丢失了音轨")
	}
}

// TestExtractSRTTextSurvivesRoundTrip 验证字幕文本经封装后仍可读回。
//
// 只探测到「有字幕流」还不够：编码不支持中文时，字幕流存在但内容是空白方块，
// 而这类问题在 ffprobe 的流信息里完全看不出来。
func TestExtractSRTTextSurvivesRoundTrip(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}

	r := newTestRunner(t, 1)
	dir := t.TempDir()
	ctx := context.Background()

	srt := filepath.Join(dir, "in.srt")
	want := "中文软字幕可读性验证"
	if err := WriteSRT(srt, []Cue{{Start: 0, End: 2, Text: want}}); err != nil {
		t.Fatalf("写字幕失败: %v", err)
	}

	clip := filepath.Join(dir, "clip.mp4")
	makeClip(t, r, clip, "320x240", 15, 2)
	norm := filepath.Join(dir, "norm.mp4")
	if err := r.Normalize(ctx, clip, norm, testSpec()); err != nil {
		t.Fatalf("归一化失败: %v", err)
	}
	out := filepath.Join(dir, "final.mp4")
	if err := r.MuxFinal(ctx, norm, "", srt, out); err != nil {
		t.Fatalf("封装字幕失败: %v", err)
	}

	// 把字幕流抽回 SRT 再比对文本。
	back := filepath.Join(dir, "back.srt")
	if err := r.run(ctx,
		"-hide_banner", "-nostdin", "-y",
		"-i", out,
		"-map", "0:s:0",
		back,
	); err != nil {
		t.Fatalf("抽取字幕流失败: %v", err)
	}

	raw, err := os.ReadFile(back)
	if err != nil {
		t.Fatalf("读回字幕失败: %v", err)
	}
	if !strings.Contains(string(raw), want) {
		t.Fatalf("字幕文本经封装后未能原样读回。\n期望包含: %s\n实际内容:\n%s", want, string(raw))
	}
}

// TestPlanCuesAndWindowsIntegration 把时间轴与字幕串起来验收：
// 生成的每条字幕都必须落在成片时长之内。
//
// 这是「转场压缩时长」与「字幕定位」两个模块之间最容易出现的接口错误：
// 两边各自单测都过，拼起来字幕却跑到成片结束之后。
func TestPlanCuesAndWindowsIntegration(t *testing.T) {
	durations := []float64{4, 4, 4}
	plan := PlanTransitions(durations, TransitionSpec{Type: TransitionFade, DurationSec: 1})
	windows := PlanShotWindows(durations, plan)
	narrations := []string{"第一段。", "第二段。", "第三段。"}

	cues := PlanCues(windows, narrations, DefaultSubtitleOptions())
	if len(cues) == 0 {
		t.Fatal("应当生成字幕")
	}
	if !plan.Enabled {
		t.Fatal("前提：本用例应启用转场")
	}
	for i, c := range cues {
		if c.End > plan.OutDuration+1e-9 {
			t.Fatalf("cue %d 终点 %.3f 超过了成片时长 %.3f —— "+
				"字幕定位没有计入转场带来的时长压缩",
				i, c.End, plan.OutDuration)
		}
	}
	// 朴素累加会得到 12 秒，转场后只有 10 秒。
	if plan.OutDuration >= 11 {
		t.Fatalf("成片时长 %.3f 未体现出转场压缩（期望约 10s）", plan.OutDuration)
	}
}

// TestNormalizeSkipsWhenSpecAlreadyMatches 确认「规格已匹配就不转码」的优化
// 不会因为探测值有微小误差而失效 —— 那会导致每个片段被无谓地重编码一次。
func TestNormalizeSkipsWhenSpecAlreadyMatches(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}

	r := newTestRunner(t, 1)
	dir := t.TempDir()
	clip := filepath.Join(dir, "src.mp4")
	makeClip(t, r, clip, "320x240", 15, 1)
	norm := filepath.Join(dir, "norm.mp4")
	if err := r.Normalize(context.Background(), clip, norm, testSpec()); err != nil {
		t.Fatalf("归一化失败: %v", err)
	}

	probe, err := r.Probe(context.Background(), norm)
	if err != nil {
		t.Fatalf("探测失败: %v", err)
	}
	// 这里复刻 handlers.go 的跳过判定，确保它能对上真实探测结果的取整方式。
	spec := testSpec()
	matched := probe.Width == spec.Width && probe.Height == spec.Height &&
		int(probe.FPS+0.5) == spec.FPS && probe.PixFmt == "yuv420p" &&
		probe.HasAudio
	if !matched {
		t.Fatalf("归一化产物未能命中「规格已匹配」判定"+
			"（%dx%d @%.3f %s hasAudio=%v），会导致重复转码",
			probe.Width, probe.Height, probe.FPS, probe.PixFmt, probe.HasAudio)
	}
	// 缺少音轨时**必须**重新归一化：handler 的判定要求 HasAudio，
	// 否则无音轨片段会混进合成流程，让 concat 错位、让 acrossfade 报错。
	if !probe.HasAudio {
		t.Fatal("归一化产物必须带音轨")
	}
}

// ---------------------------------------------------------------------------
// 纯函数：不依赖 ffmpeg
// ---------------------------------------------------------------------------

func TestParseRational(t *testing.T) {
	cases := []struct {
		in   string
		want float64
	}{
		{"30000/1001", 29.97002997002997},
		{"30/1", 30},
		{"25", 25},
		{"0/0", 0},
		{"", 0},
		{"N/A", 0},
		{"abc", 0},
	}
	for _, c := range cases {
		if got := parseRational(c.in); math.Abs(got-c.want) > 1e-6 {
			t.Errorf("parseRational(%q) = %v，期望 %v", c.in, got, c.want)
		}
	}
}

func TestParseFloat(t *testing.T) {
	for _, c := range []struct {
		in   string
		want float64
	}{{"12.5", 12.5}, {"", 0}, {"N/A", 0}, {"x", 0}} {
		if got := parseFloat(c.in); got != c.want {
			t.Errorf("parseFloat(%q) = %v，期望 %v", c.in, got, c.want)
		}
	}
}

func TestTail(t *testing.T) {
	if got := tail("abcdef", 3); got != "…def" {
		t.Errorf("tail 截断结果 = %q，期望 %q", got, "…def")
	}
	if got := tail("abc", 10); got != "abc" {
		t.Errorf("短字符串不应被截断，实际 %q", got)
	}
}

// TestWriteConcatListEscapesQuotes 覆盖 Windows 路径与单引号转义：
// concat 清单的格式错误不会报错，只会产出「时长错乱」的成片，极难排查。
func TestWriteConcatListEscapesQuotes(t *testing.T) {
	dir := t.TempDir()
	list := filepath.Join(dir, "concat.txt")
	// 文件名里放单引号：不转义会直接破坏 demuxer 的语法。
	weird := filepath.Join(dir, "it's a clip.mp4")
	files := []string{weird, filepath.Join(dir, "normal.mp4")}

	if err := WriteConcatList(list, files); err != nil {
		t.Fatalf("写清单失败: %v", err)
	}
	raw, err := os.ReadFile(list)
	if err != nil {
		t.Fatalf("读清单失败: %v", err)
	}
	content := string(raw)

	if !strings.Contains(content, `it'\''s a clip.mp4`) {
		t.Errorf("单引号未被正确转义，清单内容:\n%s", content)
	}
	// 路径分隔符必须是正斜杠。注意不能简单地断言「没有反斜杠」——
	// 引号转义序列 '\'' 本身就含反斜杠。先把合法的转义序列摘掉再检查。
	withoutEscapes := strings.ReplaceAll(content, `'\''`, "")
	if strings.Contains(withoutEscapes, `\`) {
		t.Errorf("路径分隔符未转成正斜杠，清单内容:\n%s", content)
	}
	if lines := strings.Count(content, "\n"); lines != len(files) {
		t.Errorf("清单应有 %d 行，实际 %d 行:\n%s", len(files), lines, content)
	}
}

// waitFor 轮询等待条件成立，超时则让测试失败。
func waitFor(t *testing.T, timeout time.Duration, cond func() bool, msg string) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal(msg)
}
