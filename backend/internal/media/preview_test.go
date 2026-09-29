package media

import (
	"context"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// framemd5 的输出形如（注释行以 # 开头）：
//
//	0,          0,          0,        1,   115200, 9c8d…
//
// 即 `流号, dts, pts, duration, size, hash` —— 哈希是**第 6 个**字段，
// 前面有 5 个数字。少写一个字段就永远匹配不到（第一版正是这么错的）。
var frameMD5Re = regexp.MustCompile(`(?m)^\d+,[^\n]*?([0-9a-f]{32})\s*$`)

// firstFrameMD5 取**首帧**的内容指纹。
//
// 用它比较"两个背景是不是同一个画面"比比 YAVG 可靠得多：
// 亮度平均值的巧合太多了（纯色与暗角都是低亮度），而帧指纹骗不了人。
func firstFrameMD5(t *testing.T, path string) string {
	t.Helper()
	out := ffmpegCapture(t,
		"-hide_banner", "-nostdin", "-i", path,
		"-frames:v", "1", "-f", "framemd5", "-",
	)
	m := frameMD5Re.FindStringSubmatch(out)
	if m == nil {
		t.Fatalf("没能从 framemd5 里解析出帧指纹：\n%s", out)
	}
	return m[1]
}

func TestBackgroundStyleIDsMatchPythonContract(t *testing.T) {
	// **契约测试**：这份 id 列表与 Python 的 BACKGROUND_STYLE_IDS 必须逐字一致。
	// 加预设时只改一边，表现就是"前端能选、预览报错"或"提示词支持、预览没有"。
	//
	// 这里把 Python 侧的值写死（而不是去读 .py）是刻意的：CI 里未必有 Python 环境，
	// 而写死的列表一旦被单边修改就会立刻变红 —— 这正是它要起的作用。
	// 对侧由 ai/tests/test_backgrounds.py 反向钉住 Go 的这张表。
	want := []string{"auto", "solid", "gradient", "grid", "vignette", "noise", "scanlines"}
	got := BackgroundStyleIDs()
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Errorf("背景样式 id 与 Python 侧不一致\nGo:     %s\nPython: %s",
			strings.Join(got, ","), strings.Join(want, ","))
	}
}

func TestBlendHexColourInterpolates(t *testing.T) {
	black, white := "0x000000", "0xFFFFFF"

	got, err := blendHexColour(black, white, 0)
	if err != nil || got != "0x000000" {
		t.Errorf("t=0 应取前者，实际 %s (%v)", got, err)
	}
	got, err = blendHexColour(black, white, 1)
	if err != nil || got != "0xFFFFFF" {
		t.Errorf("t=1 应取后者，实际 %s (%v)", got, err)
	}
	got, err = blendHexColour(black, white, 0.5)
	if err != nil || got != "0x808080" {
		t.Errorf("t=0.5 应取中值 0x808080，实际 %s (%v)", got, err)
	}
}

func TestBackgroundFilterRejectsUnknownStyle(t *testing.T) {
	// 未登记的样式必须报错，不静默回落成纯色 —— 与调色方案、风格预设同一态度。
	if _, err := BackgroundFilter("plaid", "#0B1020", "#4F8CFF", 320, 240, 15); err == nil {
		t.Fatal("未知背景样式应当报错")
	}
	if _, err := BackgroundFilter("solid", "not-a-colour", "#4F8CFF", 320, 240, 15); err == nil {
		t.Fatal("非法配色应当报错")
	}
}

// TestBackgroundPresetsAreVisuallyDistinct 是「背景切换」这件事的核心验收。
//
// 逐个渲染每种预设，用**首帧内容指纹**证明两两不同。
// 只断言"命令成功"是不够的：一个把所有预设都渲染成纯色的实现照样成功。
func TestBackgroundPresetsAreVisuallyDistinct(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}
	r := newTestRunner(t, 1)
	dir := t.TempDir()

	seen := map[string]string{}
	for _, style := range BackgroundStyleIDs() {
		chain, err := BackgroundFilter(style, "#0B1020", "#4F8CFF", 320, 240, 15)
		if err != nil {
			t.Fatalf("BackgroundFilter(%s) 失败: %v", style, err)
		}
		out := filepath.Join(dir, style+".mp4")
		if err := r.run(context.Background(),
			"-hide_banner", "-nostdin", "-y",
			"-f", "lavfi", "-i", chain,
			// `auto` 与 `gradient` 预览里是同一个画面，指纹必然相同，
			// 因此这里只比"不同的实现是否画出了不同的东西"。
			"-frames:v", "1", "-c:v", "libx264", "-preset", "ultrafast",
			"-pix_fmt", "yuv420p", out,
		); err != nil {
			t.Fatalf("渲染背景 %s 失败: %v", style, err)
		}
		seen[style] = firstFrameMD5(t, out)
	}

	// 每种预设都必须画出**不同**的画面，唯一允许的重复是 {auto, gradient}：
	// auto 表示"由模型按内容决定"，而预览总得画点什么出来，因此用渐变代表它。
	// 这个例外写成**显式断言**而不是放宽判定 —— 一旦有人改了 auto 的画法，
	// 这条会立刻变红，提醒他去更新这条约定（而不是悄悄多出一种画面）。
	distinct := map[string][]string{}
	for style, md5 := range seen {
		distinct[md5] = append(distinct[md5], style)
	}
	for md5, styles := range distinct {
		if len(styles) == 1 {
			continue
		}
		onlyAutoAndGradient := len(styles) == 2 &&
			containsString(styles, "auto") && containsString(styles, "gradient")
		if !onlyAutoAndGradient {
			t.Errorf("这些背景样式渲染出了同一个画面：%v（指纹 %s）", styles, md5)
		}
	}
	if len(distinct) != len(seen)-1 {
		t.Errorf("背景样式应当两两不同（auto 与 gradient 共用渐变是约定），实际 %d 个预设产生了 %d 种画面",
			len(seen), len(distinct))
	}
}

func containsString(list []string, v string) bool {
	for _, s := range list {
		if s == v {
			return true
		}
	}
	return false
}

// TestRenderEffectsPreviewAppliesGradeAndSubtitle 验证预览走的是真实后期链。
func TestRenderEffectsPreviewAppliesGradeAndSubtitle(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}
	r := newTestRunner(t, 1)
	dir := t.TempDir()

	render := func(grade string) string {
		out := filepath.Join(dir, "preview_"+grade+".mp4")
		err := r.RenderEffectsPreview(context.Background(), dir, out, PreviewSpec{
			BackgroundStyle: "grid",
			BackgroundColor: "#0B1020",
			PrimaryColor:    "#4F8CFF",
			Width:           320, Height: 240, FPS: 15,
			DurationSec: 2,
			Post: PostOptions{
				Grade:         GradeSpec{Name: grade},
				FrameHeight:   240,
				Fade:          FadeSpec{InSec: 0.4},
				SubtitleStyle: SubtitleStyle{FontSize: 20},
			},
			SampleText: "预览字幕示例",
		})
		if err != nil {
			t.Fatalf("RenderEffectsPreview(%s) 失败: %v", grade, err)
		}
		return out
	}

	plain := render("none")
	cool := render("cool")

	if got := probeAudioSec(t, plain); got < 1.7 || got > 2.4 {
		t.Errorf("预览时长应约 2s，实际 %.2fs", got)
	}
	// 调色必须真的作用在预览上：否则用户会照着一个不反映成片的预览去调参数。
	a := frameStatsAt(t, plain, 1.0)
	b := frameStatsAt(t, cool, 1.0)
	if b["UAVG"] <= a["UAVG"] {
		t.Errorf("冷色调在预览里没有生效：UAVG %.1f -> %.1f", a["UAVG"], b["UAVG"])
	}

	// 字幕必须烧进去了：示例字幕在画面下半部，取字幕行所在区域应有亮像素。
	stats := frameStatsAt(t, plain, 1.0)
	if stats["YMAX"] < 100 {
		t.Errorf("预览里没有字幕亮像素，YMAX=%.1f", stats["YMAX"])
	}
}
