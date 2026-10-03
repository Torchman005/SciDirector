package media

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

// TestPostProcessKeepsYUV420P 是"成片在 QQ/微信里播不了"的**回归测试**。
//
// 踩过的坑（用户实测反馈："为什么在 QQ 里面播放不了"）：
// 成片的 profile 是 **High 4:4:4 Predictive / yuv444p** —— 那是专业中间格式，
// QQ 影音、微信、手机、电视盒子一律播不了，而 VLC 软解能播，
// 所以开发机上完全看不出来（"我这儿能看，发出去别人打不开"）。
//
// 根因：`eq` / `curves` / `colortemperature` 在 RGB 空间工作，`subtitles`(libass)
// 也会改像素格式，于是后期处理那条滤镜链走完就成了 yuv444p；而 libx264 会
// 据此**自动选 4:4:4 Predictive**。链尾少了 `format=yuv420p`、
// 命令里也少了 `-pix_fmt yuv420p`。
//
// 这条测试只调色（不烧字幕）就足以触发 —— 正是最容易漏的那种组合。
func TestPostProcessKeepsYUV420P(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}
	r := newTestRunner(t, 1)
	dir := t.TempDir()
	src := filepath.Join(dir, "in.mp4")
	out := filepath.Join(dir, "out.mp4")
	makeColorClip(t, r, src, "gray", 2)

	// grade 会引入 eq / colortemperature —— RGB 空间的滤镜，正是它把
	// 像素格式推成 444 的。淡入淡出单独用不会触发，所以这里必须带 grade。
	if err := r.PostProcess(context.Background(), src, out, PostOptions{
		Grade: GradeSpec{Name: "cool"},
		Fade:  FadeSpec{InSec: 0.3},
	}, 2); err != nil {
		t.Fatalf("PostProcess 失败: %v", err)
	}

	probe, err := r.Probe(context.Background(), out)
	if err != nil {
		t.Fatalf("探测失败: %v", err)
	}
	if probe.PixFmt != "yuv420p" {
		t.Errorf("成片像素格式必须是 yuv420p（QQ/微信/手机才播得了），实际 %q —— "+
			"这是 High 4:4:4 Predictive 的前兆", probe.PixFmt)
	}
}

// TestPostProcessBurnKeepsYUV420P 覆盖另一条同样会改像素格式的路径：烧录字幕。
func TestPostProcessBurnKeepsYUV420P(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}
	r := newTestRunner(t, 1)
	dir := t.TempDir()
	src := filepath.Join(dir, "in.mp4")
	out := filepath.Join(dir, "out.mp4")
	srt := filepath.Join(dir, "s.srt")
	makeColorClip(t, r, src, "black", 2)

	if err := os.WriteFile(srt, []byte("1\n00:00:00,200 --> 00:00:01,800\n测试\n"), 0o644); err != nil {
		t.Fatalf("写 SRT 失败: %v", err)
	}
	if err := r.PostProcess(context.Background(), src, out, PostOptions{
		BurnSubtitlePath: srt,
		FrameHeight:      240,
	}, 2); err != nil {
		t.Fatalf("PostProcess 失败: %v", err)
	}

	probe, err := r.Probe(context.Background(), out)
	if err != nil {
		t.Fatalf("探测失败: %v", err)
	}
	if probe.PixFmt != "yuv420p" {
		t.Errorf("烧录字幕后像素格式必须仍是 yuv420p，实际 %q", probe.PixFmt)
	}
}
