package media

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"testing"
)

// ---------------------------------------------------------------------------
// 测试辅助
// ---------------------------------------------------------------------------

// ffmpegCapture 直接跑一次 ffmpeg 并把 stdout+stderr 一起收回来。
//
// 测试里刻意**不复用 Runner.run**：那个方法按设计丢弃 stdout，而这里要读的
// 正是 ffmpeg 的文本输出（volumedetect 的统计写在 stderr、
// metadata=print 写在 stdout）。复用会让"测量手段"受制于"生产实现"。
func ffmpegCapture(t *testing.T, args ...string) string {
	t.Helper()
	cmd := exec.Command("ffmpeg", args...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		t.Fatalf("ffmpeg 执行失败: %v\nargs: %v\nstderr: %s", err, args, stderr.String())
	}
	return stdout.String() + stderr.String()
}

var meanVolumeRe = regexp.MustCompile(`mean_volume:\s*(-?[\d.]+)\s*dB`)

// windowMeanVolumeDB 测某一段音频的平均电平（dBFS）。
//
// 这是本文件里最重要的测量手段：配乐闪避、淡出这些性质**只能靠电平证明**，
// 断言"命令没报错"等于什么都没验证 —— 闪避失效时命令照样成功，
// 只是旁白被配乐盖住了。
//
// 与 ffmpeg_test.go 里的 meanVolumeDB 不同：那个量整段，这个可以指定
// 时间窗口与前置滤镜 —— 闪避必须**分窗口**比较才看得出来。
func windowMeanVolumeDB(t *testing.T, path string, ss, dur float64, af string) float64 {
	t.Helper()
	// volumedetect 必须挂在**滤镜链末尾**：想只看某个频段（例如配乐的 220 Hz）
	// 就得先带通再测电平，否则测到的是"旁白 + 配乐"的总和，
	// 而总和在说话时反而更大 —— 会把"闪避正常"读成"闪避失效"。
	filter := "volumedetect"
	if af != "" {
		filter = af + ",volumedetect"
	}

	args := []string{"-hide_banner", "-nostdin"}
	if ss > 0 {
		args = append(args, "-ss", fmt.Sprintf("%.3f", ss))
	}
	args = append(args, "-t", fmt.Sprintf("%.3f", dur), "-i", path,
		"-af", filter, "-f", "null", "-")

	out := ffmpegCapture(t, args...)
	m := meanVolumeRe.FindStringSubmatch(out)
	if m == nil {
		t.Fatalf("没能从 volumedetect 输出里解析出 mean_volume：\n%s", out)
	}
	v, err := strconv.ParseFloat(m[1], 64)
	if err != nil {
		t.Fatalf("解析 mean_volume %q 失败: %v", m[1], err)
	}
	return v
}

var signalStatRe = regexp.MustCompile(`lavfi\.signalstats\.(\w+)=(-?[\d.]+)`)

// frameStatsAt 取指定时刻那一帧的 signalstats（YUV 三通道的平均/极值）。
//
// 用它来证明画面真的变了：调色与淡入淡出最终都要落到像素上，
// 而"输出文件大小变了"这种断言在内容没变时也可能成立。
func frameStatsAt(t *testing.T, path string, ss float64) map[string]float64 {
	t.Helper()
	args := []string{"-hide_banner", "-nostdin"}
	if ss > 0 {
		args = append(args, "-ss", fmt.Sprintf("%.3f", ss))
	}
	args = append(args, "-i", path,
		"-vf", "signalstats,metadata=print:file=-",
		"-frames:v", "1", "-f", "null", "-")

	out := ffmpegCapture(t, args...)
	stats := map[string]float64{}
	for _, m := range signalStatRe.FindAllStringSubmatch(out, -1) {
		v, err := strconv.ParseFloat(m[2], 64)
		if err == nil {
			stats[m[1]] = v
		}
	}
	if len(stats) == 0 {
		t.Fatalf("没能从 signalstats 里解析出任何统计值：\n%s", out)
	}
	return stats
}

// makeColorClip 用 lavfi 生成一段纯色视频（像素统计可预期，便于断言）。
func makeColorClip(t *testing.T, r *Runner, out, color string, seconds float64) {
	t.Helper()
	if err := r.run(context.Background(),
		"-hide_banner", "-nostdin", "-y",
		"-f", "lavfi", "-i", fmt.Sprintf("color=c=%s:s=320x240:r=15:d=%.3f", color, seconds),
		"-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", out,
	); err != nil {
		t.Fatalf("生成纯色片段失败: %v", err)
	}
}

// makeEvalAudio 用 aevalsrc 生成一段音频。expr 直接写表达式，便于造出
// "说话时响、停顿时静"这类**时间上有结构**的信号 —— 闪避测试依赖它。
func makeEvalAudio(t *testing.T, r *Runner, out, expr string, seconds float64) {
	t.Helper()
	if err := r.run(context.Background(),
		"-hide_banner", "-nostdin", "-y",
		"-f", "lavfi",
		"-i", fmt.Sprintf("aevalsrc=%s:d=%.3f:s=48000", expr, seconds),
		"-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", out,
	); err != nil {
		t.Fatalf("生成测试音频失败: %v", err)
	}
}

// ---------------------------------------------------------------------------
// 纯函数：调色方案
// ---------------------------------------------------------------------------

func TestPlanGradeRejectsUnknownName(t *testing.T) {
	// 静默降级在这里特别危险：用户选了"胶片感"，成片却是原色，
	// 而没有任何地方提示过。
	_, err := PlanGrade("sepia", 1)
	if err == nil {
		t.Fatal("未知调色方案应当报错，而不是静默按原色处理")
	}
	if !strings.Contains(err.Error(), "none") {
		t.Errorf("错误信息应当列出可用方案，实际: %v", err)
	}
}

func TestPlanGradeNoneProducesNoFilter(t *testing.T) {
	for _, name := range []string{"", "none", "NONE", "  none  "} {
		got, err := PlanGrade(name, 1)
		if err != nil {
			t.Fatalf("PlanGrade(%q) 不该报错: %v", name, err)
		}
		if got != "" {
			t.Errorf("PlanGrade(%q) 应返回空滤镜串，实际 %q", name, got)
		}
	}
}

func TestPlanGradeStrengthInterpolates(t *testing.T) {
	half, err := PlanGrade("high_contrast", 0.5)
	if err != nil {
		t.Fatalf("PlanGrade 失败: %v", err)
	}
	full, err := PlanGrade("high_contrast", 1)
	if err != nil {
		t.Fatalf("PlanGrade 失败: %v", err)
	}
	if half == full {
		t.Fatal("强度 0.5 与 1.0 应当产生不同的滤镜串")
	}
	// 0.5 的对比度必须落在「1.0（不变）」与「1.20（拉满）」之间。
	cHalf := parseEqParam(t, half, "contrast")
	cFull := parseEqParam(t, full, "contrast")
	if !(cHalf > 1.0 && cHalf < cFull) {
		t.Errorf("强度插值不对：contrast(0.5)=%.4f 应落在 (1, %.4f) 之间", cHalf, cFull)
	}
}

func TestPlanGradeZeroStrengthMeansFull(t *testing.T) {
	// 语义刻意如此：0 与 1 都表示"完整效果"，关掉请用 none。
	// 这样"JSON 零值（忘了填）"与"明确关闭"就不会混为一谈。
	zero, err := PlanGrade("cool", 0)
	if err != nil {
		t.Fatalf("PlanGrade 失败: %v", err)
	}
	full, err := PlanGrade("cool", 1)
	if err != nil {
		t.Fatalf("PlanGrade 失败: %v", err)
	}
	if zero != full {
		t.Errorf("strength=0 应按完整效果处理\n0 -> %q\n1 -> %q", zero, full)
	}
}

// parseEqParam 从 `eq=contrast=1.0600:brightness=...` 里取出某个参数。
func parseEqParam(t *testing.T, chain, key string) float64 {
	t.Helper()
	idx := strings.Index(chain, "eq=")
	if idx < 0 {
		t.Fatalf("滤镜串里没有 eq：%q", chain)
	}
	for _, part := range strings.Split(chain[idx+3:], ":") {
		k, v, ok := strings.Cut(part, "=")
		if ok && k == key {
			f, err := strconv.ParseFloat(v, 64)
			if err != nil {
				t.Fatalf("解析 %s=%s 失败: %v", key, v, err)
			}
			return f
		}
	}
	t.Fatalf("滤镜串里没有 %s：%q", key, chain)
	return 0
}

// ---------------------------------------------------------------------------
// 纯函数：滤镜链顺序与开关
// ---------------------------------------------------------------------------

func TestPlanVideoChainOrdersGradeSubtitleFade(t *testing.T) {
	chain, err := PlanVideoChain(PostOptions{
		Grade:            GradeSpec{Name: "cool"},
		Fade:             FadeSpec{InSec: 0.5, OutSec: 1.0},
		BurnSubtitlePath: "ignored",
	}, 10, "final.srt")
	if err != nil {
		t.Fatalf("PlanVideoChain 失败: %v", err)
	}
	iGrade := strings.Index(chain, "colortemperature")
	iSub := strings.Index(chain, "subtitles=")
	iFade := strings.Index(chain, "fade=t=in")
	if iGrade < 0 || iSub < 0 || iFade < 0 {
		t.Fatalf("三段效果都应当出现：%q", chain)
	}
	if !(iGrade < iSub && iSub < iFade) {
		t.Errorf("顺序必须是 调色 -> 字幕 -> 淡入淡出，实际：%q", chain)
	}
}

func TestPlanVideoChainClampsFadeOutStart(t *testing.T) {
	// 淡出比整片还长时，起点必须夹到 0，否则 st 为负会让 fade 完全失效
	// （ffmpeg 不报错，只是什么都不做）。
	chain, err := PlanVideoChain(PostOptions{Fade: FadeSpec{OutSec: 5}}, 2, "")
	if err != nil {
		t.Fatalf("PlanVideoChain 失败: %v", err)
	}
	if !strings.Contains(chain, "st=0.000") {
		t.Errorf("淡出起点应为 0，实际滤镜串：%q", chain)
	}
}

func TestNeedsPostProcess(t *testing.T) {
	cases := []struct {
		name string
		opts PostOptions
		want bool
	}{
		{"什么都不请求", PostOptions{}, false},
		{"grade=none", PostOptions{Grade: GradeSpec{Name: "none"}}, false},
		{"grade=cool", PostOptions{Grade: GradeSpec{Name: "cool"}}, true},
		{"淡入", PostOptions{Fade: FadeSpec{InSec: 0.5}}, true},
		{"淡出", PostOptions{Fade: FadeSpec{OutSec: 0.5}}, true},
		{"烧录字幕", PostOptions{BurnSubtitlePath: "x.srt"}, true},
	}
	for _, c := range cases {
		if got := NeedsPostProcess(c.opts); got != c.want {
			t.Errorf("%s: NeedsPostProcess=%v，期望 %v", c.name, got, c.want)
		}
	}
}

// ---------------------------------------------------------------------------
// 真实 ffmpeg：后期处理
// ---------------------------------------------------------------------------

func TestPostProcessRefusesWhenNothingRequested(t *testing.T) {
	requireFFmpeg(t)
	r := newTestRunner(t, 1)
	dir := t.TempDir()
	src := filepath.Join(dir, "in.mp4")
	makeColorClip(t, r, src, "white", 2)

	// 调用方应当先问 NeedsPostProcess；直接调用必须报错而不是白跑一次重编码。
	err := r.PostProcess(context.Background(), src, filepath.Join(dir, "out.mp4"), PostOptions{}, 2)
	if err == nil {
		t.Fatal("没有任何效果时不该执行后期编码")
	}
}

func TestPostProcessFadeInDarkensFirstFrame(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}
	r := newTestRunner(t, 1)
	dir := t.TempDir()
	src := filepath.Join(dir, "in.mp4")
	out := filepath.Join(dir, "out.mp4")
	makeColorClip(t, r, src, "white", 2)

	before := frameStatsAt(t, src, 0)
	if err := r.PostProcess(context.Background(), src, out, PostOptions{
		Fade: FadeSpec{InSec: 0.8},
	}, 2); err != nil {
		t.Fatalf("PostProcess 失败: %v", err)
	}
	after := frameStatsAt(t, out, 0)

	// 白色片段淡入 0.8s，第一帧应当几乎是黑的。
	if after["YAVG"] > before["YAVG"]/4 {
		t.Errorf("淡入没有生效：首帧亮度 %.1f -> %.1f（期望显著变暗）",
			before["YAVG"], after["YAVG"])
	}
}

func TestPostProcessGradeShiftsColorBalance(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}
	r := newTestRunner(t, 1)
	dir := t.TempDir()
	src := filepath.Join(dir, "in.mp4")
	makeColorClip(t, r, src, "gray", 2)
	before := frameStatsAt(t, src, 0)

	// 两个方向都要验，缺一不可：只测"冷变蓝"的话，
	// 一个把画面整体推向蓝色（而不分冷暖）的实现也能通过。
	// 这正是本项目反复强调的**反向对照**。
	cases := []struct {
		grade string
		want  string // UAVG 应当上升还是下降
	}{
		{"cool", "上升"},
		{"warm", "下降"},
	}
	for _, c := range cases {
		out := filepath.Join(dir, c.grade+".mp4")
		if err := r.PostProcess(context.Background(), src, out, PostOptions{
			Grade: GradeSpec{Name: c.grade},
		}, 2); err != nil {
			t.Fatalf("PostProcess(%s) 失败: %v", c.grade, err)
		}
		after := frameStatsAt(t, out, 0)
		rose := after["UAVG"] > before["UAVG"]
		wantRose := c.want == "上升"
		if rose != wantRose {
			t.Errorf("%s 色调没有生效：UAVG %.1f -> %.1f（期望%s）",
				c.grade, before["UAVG"], after["UAVG"], c.want)
		}
	}
}

// TestPostProcessBurnsSubtitleOnWindowsStylePath 钉住烧录字幕的路径处理。
//
// 这条用例针对的是一个具体的坑：`subtitles=` 是**滤镜参数**，而滤镜图有自己的
// 语法（冒号分隔选项）。Windows 的 `C:` 落进去会被当成选项分隔符，
// 而转义写法在不同 ffmpeg 版本上行为不一致。实现改用「把工作目录设成字幕目录、
// 参数只传文件名」，因此这里必须用**绝对路径**起一个临时目录来验证它成立。
func TestPostProcessBurnsSubtitleOnWindowsStylePath(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}
	r := newTestRunner(t, 1)
	dir := t.TempDir()
	src := filepath.Join(dir, "in.mp4")
	out := filepath.Join(dir, "out.mp4")
	srt := filepath.Join(dir, "final.srt")
	makeColorClip(t, r, src, "black", 2)

	srtBody := "1\n00:00:00,200 --> 00:00:01,800\n烧录字幕测试\n"
	if err := os.WriteFile(srt, []byte(srtBody), 0o644); err != nil {
		t.Fatalf("写 SRT 失败: %v", err)
	}

	if err := r.PostProcess(context.Background(), src, out, PostOptions{
		BurnSubtitlePath: srt,
	}, 2); err != nil {
		t.Fatalf("烧录字幕失败（很可能是滤镜参数里的路径没处理好）: %v", err)
	}
	if _, err := os.Stat(out); err != nil {
		t.Fatalf("烧录后没有产物: %v", err)
	}
	// 字幕在 0.2~1.8s 之间出现，因此要看 1.0s 那一帧 ——
	// 取第 0 帧永远测不到（那时字幕还没开始）。
	stats := frameStatsAt(t, out, 1.0)
	if stats["YMAX"] <= 0 {
		t.Errorf("画面里没有任何亮像素，字幕很可能没烧进去：%+v", stats)
	}
}

// ---------------------------------------------------------------------------
// 真实 ffmpeg：配乐
// ---------------------------------------------------------------------------

func TestBuildBgmTrackLoopsShortAudioToTarget(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}
	r := newTestRunner(t, 1)
	dir := t.TempDir()
	bgm := filepath.Join(dir, "bgm.m4a")
	out := filepath.Join(dir, "bgm_track.m4a")
	makeEvalAudio(t, r, bgm, "0.4*sin(2*PI*220*t)", 1.0)

	if err := r.BuildBgmTrack(context.Background(), bgm, out, 4.0, BgmSpec{Loop: true}); err != nil {
		t.Fatalf("BuildBgmTrack 失败: %v", err)
	}
	got := probeAudioSec(t, out)
	// 1 秒素材循环到 4 秒。产出的轨会比目标**略长**（bgmTailMarginSec），
	// 那是刻意的：短一点就是"结尾没声音"，长一点会被下游 -shortest 裁掉。
	if got < 4.0 {
		t.Errorf("配乐轨不得短于目标 4s，实际 %.2fs（循环没生效？）", got)
	}
	if got > 4.0+bgmTailMarginSec+0.3 {
		t.Errorf("配乐轨超出目标太多：%.2fs（余量应只有 %.1fs）", got, bgmTailMarginSec)
	}
}

func TestBuildBgmTrackTrimsLongerAudio(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}
	r := newTestRunner(t, 1)
	dir := t.TempDir()
	bgm := filepath.Join(dir, "bgm.m4a")
	out := filepath.Join(dir, "bgm_track.m4a")
	makeEvalAudio(t, r, bgm, "0.4*sin(2*PI*220*t)", 5.0)

	if err := r.BuildBgmTrack(context.Background(), bgm, out, 2.0, BgmSpec{Loop: false}); err != nil {
		t.Fatalf("BuildBgmTrack 失败: %v", err)
	}
	got := probeAudioSec(t, out)
	if got < 2.0 || got > 2.0+bgmTailMarginSec+0.3 {
		t.Errorf("配乐应被裁到 [2s, 2s+余量]，实际 %.2fs", got)
	}
}

// TestBuildBgmTrackTailIsNotSilent 是本组里最该存在的一条。
//
// 它守的是用户直接提出来的要求：**配乐要循环铺满，不能"视频长了后面没有 BGM"**。
// 关键在于断言的是**内容**而不是时长 —— 一个时长完全正确、后半段却是静音的实现
// 能通过所有"时长约等于目标"的用例，而它正是用户听到的那个缺陷。
func TestBuildBgmTrackTailIsNotSilent(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}
	r := newTestRunner(t, 1)
	dir := t.TempDir()
	bgm := filepath.Join(dir, "bgm.m4a")
	out := filepath.Join(dir, "bgm_track.m4a")
	// 1.5 秒素材循环到 6 秒：后面 4.5 秒**全部**来自循环。
	makeEvalAudio(t, r, bgm, "0.5*sin(2*PI*220*t)", 1.5)

	if err := r.BuildBgmTrack(context.Background(), bgm, out, 6.0, BgmSpec{Loop: true}); err != nil {
		t.Fatalf("BuildBgmTrack 失败: %v", err)
	}

	for _, c := range []struct {
		name string
		ss   float64
	}{{"开头", 0.2}, {"中段", 2.4}, {"结尾", 5.2}} {
		if lv := windowMeanVolumeDB(t, out, c.ss, 0.6, ""); lv < -60 {
			t.Errorf("%s 是静音（%.1f dB）—— 配乐没有铺满整片", c.name, lv)
		}
	}

	// 反向对照：同一套测量对**真的静音**必须能读出来。
	// 少了它，上面三条断言在"测量函数永远返回一个不小的数"时同样会通过。
	// 这里用带淡出的同一次构建：淡出结束于 6.0s，而轨长 6.5s，
	// 所以 [6.2, 6.4] 是确定无疑的数字静音。
	faded := filepath.Join(dir, "bgm_faded.m4a")
	if err := r.BuildBgmTrack(context.Background(), bgm, faded, 6.0, BgmSpec{
		Loop: true, FadeOutSec: 1.0,
	}); err != nil {
		t.Fatalf("BuildBgmTrack 失败: %v", err)
	}
	if lv := windowMeanVolumeDB(t, faded, 6.2, 0.2, ""); lv > -60 {
		t.Errorf("反向对照失败：淡出之后的窗口读到了 %.1f dB，测量手段不可信", lv)
	}
}

// TestMixSoundtrackSpansLongestInput 钉住"混音只允许变长、不允许变短"。
//
// 原先用的是 `amix=duration=first`，而 first 是**旁白**轨 —— 只要旁白比配乐短，
// 配乐就被截断在那里，接着 `MuxFinal` 的 `-shortest` 还会把成片一起截短。
// 两个后果都不会报错：一个是"结尾没配乐"，一个是整片变短。
func TestMixSoundtrackSpansLongestInput(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}
	r := newTestRunner(t, 1)
	dir := t.TempDir()
	voice := filepath.Join(dir, "voice.m4a")
	bgm := filepath.Join(dir, "bgm.m4a")
	out := filepath.Join(dir, "mixed.m4a")
	makeEvalAudio(t, r, voice, "0.7*sin(2*PI*2000*t)", 2.0) // 旁白短
	makeEvalAudio(t, r, bgm, "0.5*sin(2*PI*220*t)", 5.0)    // 配乐长

	if err := r.MixSoundtrack(context.Background(), voice, bgm, out, 0); err != nil {
		t.Fatalf("MixSoundtrack 失败: %v", err)
	}
	got := probeAudioSec(t, out)
	if got < 4.5 {
		t.Errorf("混音应覆盖较长的配乐（~5s），实际 %.2fs —— 被较短的旁白截断了", got)
	}
	// 配乐在旁白结束之后必须仍有声音（这正是"结尾没有 BGM"的判据）。
	if lv := windowMeanVolumeDB(t, out, 4.2, 0.5, ""); lv < -60 {
		t.Errorf("旁白结束后的窗口是静音（%.1f dB）—— 配乐没有覆盖到结尾", lv)
	}
}

func TestBuildBgmTrackFadesOut(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}
	r := newTestRunner(t, 1)
	dir := t.TempDir()
	bgm := filepath.Join(dir, "bgm.m4a")
	out := filepath.Join(dir, "bgm_track.m4a")
	makeEvalAudio(t, r, bgm, "0.5*sin(2*PI*220*t)", 6.0)

	if err := r.BuildBgmTrack(context.Background(), bgm, out, 6.0, BgmSpec{
		Loop: false, FadeInSec: 0.5, FadeOutSec: 2.0,
	}); err != nil {
		t.Fatalf("BuildBgmTrack 失败: %v", err)
	}

	mid := windowMeanVolumeDB(t, out, 2.0, 1.0, "")
	tail := windowMeanVolumeDB(t, out, 5.6, 0.3, "")
	if !(tail < mid-6) {
		t.Errorf("淡出没有生效：中段 %.1f dB，末尾 %.1f dB（期望末尾低至少 6 dB）", mid, tail)
	}
}

// TestMixSoundtrackDucksBgmUnderVoice 是配乐功能**最核心的一条验收**。
//
// 测量办法：把配乐固定成 220 Hz、旁白固定成 2000 Hz，然后用 220 Hz 的带通
// 只看配乐那一路的电平 —— 这样"旁白在说话"就不会污染读数。
// 说话窗口里的配乐电平必须显著低于停顿窗口，否则就是"旁白被配乐盖住"，
// 而那正是加配乐最容易翻车的地方（命令全部成功、听起来就是听不清）。
func TestMixSoundtrackDucksBgmUnderVoice(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}
	r := newTestRunner(t, 1)
	dir := t.TempDir()
	voice := filepath.Join(dir, "voice.m4a")
	bgm := filepath.Join(dir, "bgm.m4a")
	out := filepath.Join(dir, "mixed.m4a")

	// 旁白：0~1s 说话、1~2s 停顿、2~3s 说话、3~4s 停顿。
	makeEvalAudio(t, r, voice, "0.7*sin(2*PI*2000*t)*lt(mod(t\\,2)\\,1)", 4.0)
	// 配乐：全程恒定的 220 Hz。
	makeEvalAudio(t, r, bgm, "0.5*sin(2*PI*220*t)", 4.0)

	if err := r.MixSoundtrack(context.Background(), voice, bgm, out, 0); err != nil {
		t.Fatalf("MixSoundtrack 失败: %v", err)
	}

	const bandpass = "bandpass=f=220:width_type=h:w=40"
	// 反向对照：配乐**源**在这两个窗口里电平应当基本一致，
	// 否则说明带通测量本身受窗口位置影响，后面的结论就不成立。
	srcSpeak := windowMeanVolumeDB(t, bgm, 0.2, 0.6, bandpass)
	srcPause := windowMeanVolumeDB(t, bgm, 1.2, 0.6, bandpass)
	if diff := srcSpeak - srcPause; diff > 2 || diff < -2 {
		t.Fatalf("反向对照失败：配乐源本身在两个窗口就不一致（%.1f vs %.1f dB），测量手段不可信",
			srcSpeak, srcPause)
	}

	mixSpeak := windowMeanVolumeDB(t, out, 0.2, 0.6, bandpass)
	mixPause := windowMeanVolumeDB(t, out, 1.2, 0.6, bandpass)
	if !(mixSpeak < mixPause-6) {
		t.Errorf("闪避没有生效：说话时配乐 %.1f dB、停顿时 %.1f dB（期望说话时低至少 6 dB）",
			mixSpeak, mixPause)
	}
}

func TestMixSoundtrackVoiceOnlyIsAccepted(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}
	r := newTestRunner(t, 1)
	dir := t.TempDir()
	voice := filepath.Join(dir, "voice.m4a")
	out := filepath.Join(dir, "mixed.m4a")
	makeEvalAudio(t, r, voice, "0.7*sin(2*PI*2000*t)", 3.0)

	// 只有旁白（没有配乐）也必须走得通：这是"用户没选 BGM"的正常路径。
	if err := r.MixSoundtrack(context.Background(), voice, "", out, 0); err != nil {
		t.Fatalf("只有旁白时混音失败: %v", err)
	}
	got := probeAudioSec(t, out)
	if got < 2.5 || got > 3.5 {
		t.Errorf("混音时长应 ~3s，实际 %.2fs", got)
	}
}

func TestMixSoundtrackRejectsEmptyInputs(t *testing.T) {
	r := newTestRunner(t, 1)
	err := r.MixSoundtrack(context.Background(), "", "", filepath.Join(t.TempDir(), "x.m4a"), 0)
	if err == nil {
		t.Fatal("两路音轨都为空时应当报错，而不是产出一个空文件")
	}
}

func TestFitFadesNeverOverlaps(t *testing.T) {
	// 目标 3s、淡入 2s + 淡出 2s：不收紧的话两段会重叠，
	// ffmpeg 不报错，但听感是"一直在淡入"。
	in, out := fitFades(2, 2, 3)
	if in+out > 3+1e-9 {
		t.Errorf("淡入淡出之和 %.2f 超过了目标时长 3s", in+out)
	}
	if in <= 0 || out <= 0 {
		t.Errorf("按比例缩放不该把某一段压成 0：in=%.2f out=%.2f", in, out)
	}
}

// ---------------------------------------------------------------------------
// 字幕样式
// ---------------------------------------------------------------------------

func TestPlanSubtitleStyleUsesAssBGRColourOrder(t *testing.T) {
	// ASS 的颜色是 &HAABBGGRR —— 与 #RRGGBB 相比红蓝是**反的**。
	// 照直觉写成 &H00RRGGBB 不会报错，只会让红色显示成蓝色，
	// 而这种"配色不对"极难从成片反推回配置。所以这里逐个钉住。
	cases := map[string]string{
		"#FF0000": "&H000000FF", // 纯红：R=FF 必须落在最后
		"#0000FF": "&H00FF0000", // 纯蓝：B=FF 必须落在最前
		"#00FF00": "&H0000FF00", // 纯绿在中间，两种写法相同 —— 只测它测不出错误
		"#FFFFFF": "&H00FFFFFF",
		"#0B1020": "&H0020100B",
	}
	for in, want := range cases {
		got, err := assColour(in)
		if err != nil {
			t.Fatalf("assColour(%q) 报错: %v", in, err)
		}
		if got != want {
			t.Errorf("assColour(%q) = %s，期望 %s", in, got, want)
		}
	}
}

func TestPlanSubtitleStyleRejectsBadColour(t *testing.T) {
	for _, bad := range []string{"red", "#FFF", "#GGGGGG", "ABCDEF", "#FF00000"} {
		if _, err := assColour(bad); err == nil {
			t.Errorf("颜色 %q 应当被拒绝", bad)
		}
	}
}

// subtitleFontSizePx 从样式串里把字号**换算回像素**。
//
// 必须换算回来才能断言"自动值随画面高度变化"：ASS 的 FontSize 是脚本坐标，
// 720p 与 1080p 下的脚本单位可能巧合地相同（都取整到 10），
// 但换算成像素后分别约 25px 与约 37px —— 差异正发生在这一层。
func subtitleFontSizePx(t *testing.T, style string, frameHeight int) float64 {
	t.Helper()
	m := regexp.MustCompile(`FontSize=([\d.]+)`).FindStringSubmatch(style)
	if m == nil {
		t.Fatalf("样式串里没有 FontSize：%s", style)
	}
	units, err := strconv.ParseFloat(m[1], 64)
	if err != nil {
		t.Fatalf("解析 FontSize 失败: %v", err)
	}
	return units * float64(frameHeight) / srtToAssPlayResY
}

func TestPlanSubtitleStyleAutoScalesWithFrameHeight(t *testing.T) {
	// **这条测的是"字号真的是像素"**，而不是"样式串长什么样"。
	//
	// 背景（实测）：libass 把 SRT 转 ASS 时用默认 PlayResY=288，所以 1080p 下
	// FontSize 会被放大 3.75 倍 —— 请求 45px 实际渲染出约 169px 的字
	// （6 个汉字宽约 800px）。少了这层换算，用户只会觉得"怎么调都太大"。
	cases := []struct {
		height int
		wantPx float64
	}{
		{720, 24},
		{1080, 36},
		{2160, 72},
	}
	for _, c := range cases {
		style, err := PlanSubtitleStyle(SubtitleStyle{}, c.height)
		if err != nil {
			t.Fatalf("PlanSubtitleStyle(%d) 报错: %v", c.height, err)
		}
		gotPx := subtitleFontSizePx(t, style, c.height)
		if diff := gotPx - c.wantPx; diff > 2 || diff < -2 {
			t.Errorf("%dp 的自动字号应约 %.0fpx，实际 %.1fpx（样式串：%s）",
				c.height, c.wantPx, gotPx, style)
		}
	}

	// 用户显式给像素值时，也必须如实落到像素上（这是 API 文档的承诺）。
	style, err := PlanSubtitleStyle(SubtitleStyle{FontSize: 48}, 1080)
	if err != nil {
		t.Fatalf("PlanSubtitleStyle 报错: %v", err)
	}
	if got := subtitleFontSizePx(t, style, 1080); got < 47 || got > 49 {
		t.Errorf("显式 font_size=48 应当渲染成约 48px，实际 %.1fpx（%s）", got, style)
	}

	// 没有画面高度时不能崩，也不能产出空样式让字幕变成 libass 的默认大小。
	fallback, err := PlanSubtitleStyle(SubtitleStyle{}, 0)
	if err != nil {
		t.Fatalf("frameHeight=0 时不该报错: %v", err)
	}
	if !strings.Contains(fallback, "FontSize=") {
		t.Errorf("缺少画面高度时也应当给出可用样式，实际：%s", fallback)
	}
}

func TestPlanSubtitleStyleAlwaysCarriesAnOutline(t *testing.T) {
	// 描边不是装饰：字幕压在浅色画面上时，没有描边就是一片糊。
	// 因此即便用户把 outline_width 留成 0（"自动"），也必须产出非零描边。
	got, err := PlanSubtitleStyle(SubtitleStyle{}, 1080)
	if err != nil {
		t.Fatalf("PlanSubtitleStyle 报错: %v", err)
	}
	if strings.Contains(got, "Outline=0.0") || !strings.Contains(got, "Outline=") {
		t.Errorf("自动描边不该为 0，实际：%s", got)
	}
}

// TestPostProcessSubtitleFontSizeChangesTheFrame 用像素证明字号真的生效。
//
// 黑底白字下，字越大 -> 白像素越多 -> 整帧平均亮度越高。
// 这条同时验证了 force_style 能被 libass 正确解析 ——
// force_style 的值里含**逗号**，而逗号在滤镜图里是滤镜分隔符，
// 少一层引号整条链就会被劈开，ffmpeg 只报一句含糊的 Invalid argument。
func TestPostProcessSubtitleFontSizeChangesTheFrame(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}
	r := newTestRunner(t, 1)
	dir := t.TempDir()
	src := filepath.Join(dir, "in.mp4")
	srt := filepath.Join(dir, "final.srt")
	makeColorClip(t, r, src, "black", 2)

	srtBody := "1\n00:00:00,200 --> 00:00:01,800\n字号测试 ABC\n"
	if err := os.WriteFile(srt, []byte(srtBody), 0o644); err != nil {
		t.Fatalf("写 SRT 失败: %v", err)
	}

	render := func(fontSize int) float64 {
		out := filepath.Join(dir, fmt.Sprintf("font%d.mp4", fontSize))
		err := r.PostProcess(context.Background(), src, out, PostOptions{
			BurnSubtitlePath: srt,
			FrameHeight:      240, // 测试片段是 320x240
			SubtitleStyle:    SubtitleStyle{FontSize: fontSize},
		}, 2)
		if err != nil {
			t.Fatalf("烧录字幕（字号 %d）失败: %v", fontSize, err)
		}
		return frameStatsAt(t, out, 1.0)["YAVG"]
	}

	small := render(14)
	large := render(64)
	if large <= small {
		t.Errorf("字号变大后画面亮度没有变化：14px -> %.2f，64px -> %.2f（force_style 没生效？）",
			small, large)
	}
}
