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
	return NewRunner(config.MediaConfig{
		FFmpegBin:      "ffmpeg",
		FFprobeBin:     "ffprobe",
		MaxParallel:    maxParallel,
		CommandTimeout: 2 * time.Minute,
	})
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
		if err := r.Normalize(ctx, in, out, wantW, wantH, wantFPS); err != nil {
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
	if err := r.Normalize(ctx, quoted, normQuoted, 320, 240, 15); err != nil {
		t.Fatalf("归一化含单引号路径失败: %v", err)
	}
	if err := r.Normalize(ctx, plain, normPlain, 320, 240, 15); err != nil {
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
	if err := r.Normalize(context.Background(), clip, norm, 320, 240, 15); err != nil {
		t.Fatalf("归一化失败: %v", err)
	}

	probe, err := r.Probe(context.Background(), norm)
	if err != nil {
		t.Fatalf("探测失败: %v", err)
	}
	// 这里复刻 handlers.go 的跳过判定，确保它能对上真实探测结果的取整方式。
	matched := probe.Width == 320 && probe.Height == 240 &&
		int(probe.FPS+0.5) == 15 && probe.PixFmt == "yuv420p"
	if !matched {
		t.Fatalf("归一化产物未能命中「规格已匹配」判定（%dx%d @%.3f %s），"+
			"会导致重复转码", probe.Width, probe.Height, probe.FPS, probe.PixFmt)
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
