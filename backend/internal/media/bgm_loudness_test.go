package media

import (
	"context"
	"fmt"
	"path/filepath"
	"regexp"
	"strconv"
	"testing"
)

// 用户反馈「默认 BGM 音量太大」。根因不是默认值选得不好，而是音频链路：
//
//  1. 上传的音乐文件响度千差万别，而 `volume_db` 是直接乘在**原始文件**上的，
//     于是同一个滑块在不同文件上效果完全不同（"时灵时不灵"）；
//  2. MixSoundtrack 最后统一过 `loudnorm=I=-16` —— 那是**对白**口径，
//     纯 BGM 的成片因此被直接拽到人声响度。
//
// 修法：配乐先归一到 -20 LUFS 基准，`volume_db` 变成相对偏移；
// 纯配乐不再过最后那道对白口径的归一。下面用**实测 LUFS** 钉住这两条。

var integratedLUFSRe = regexp.MustCompile(`I:\s*(-?[\d.]+)\s*LUFS`)

// integratedLUFS 用 ebur128 测整段综合响度（LUFS）。
//
// 用 LUFS 而不是 volumedetect 的 mean_volume：后者是无加权的平均电平，
// 与人耳感受差得远；而这次要控的就是响度本身，必须测同一个量。
func integratedLUFS(t *testing.T, path string) float64 {
	t.Helper()
	out := ffmpegCapture(t,
		"-hide_banner", "-nostdin", "-i", path,
		"-af", "ebur128=framelog=quiet", "-f", "null", "-",
	)
	// 取**最后一条** I: —— 综合响度是逐秒累积的，最后一行才是整段结果。
	all := integratedLUFSRe.FindAllStringSubmatch(out, -1)
	if len(all) == 0 {
		t.Fatalf("没能从 ebur128 里解析出综合响度：\n%s", out)
	}
	v, err := strconv.ParseFloat(all[len(all)-1][1], 64)
	if err != nil {
		t.Fatalf("解析 LUFS %q 失败: %v", all[len(all)-1][1], err)
	}
	return v
}

// makeLoudTone 造一段**很响**的音乐源，用来证明"基准归一"真的在起作用。
//
// 刻意做成 -3 dBFS 左右：这正是真实音乐文件常见的电平，
// 也是"直接乘 volume_db"会失真的那种输入。
func makeLoudTone(t *testing.T, r *Runner, out string, seconds float64) {
	t.Helper()
	makeEvalAudio(t, r, out, "0.7*sin(2*PI*220*t)", seconds)
}

func TestBgmTrackIsNormalisedToReferenceLoudness(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}
	r := newTestRunner(t, 1)
	dir := t.TempDir()
	src := filepath.Join(dir, "loud.m4a")
	out := filepath.Join(dir, "bgm.m4a")
	makeLoudTone(t, r, src, 5)

	source := integratedLUFS(t, src)
	if err := r.BuildBgmTrack(context.Background(), src, out, 5, BgmSpec{}); err != nil {
		t.Fatalf("BuildBgmTrack 失败: %v", err)
	}
	got := integratedLUFS(t, out)

	// 源本身很响，输出必须落到基准附近（容忍 loudnorm 单遍的 ±1.5 LU 误差）。
	if diff := got - bgmReferenceLUFS; diff > 1.5 || diff < -1.5 {
		t.Errorf("配乐应被归一到基准 %.0f LUFS，实际 %.1f LUFS（源 %.1f LUFS）",
			bgmReferenceLUFS, got, source)
	}
	// 同时证明"源确实很响"，否则这条测试可能是碰巧通过的。
	if source < -8 {
		t.Errorf("测试前提不成立：源只有 %.1f LUFS，不够响（应约 -3）", source)
	}
}

func TestBgmVolumeOffsetIsRelativeToReference(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}
	r := newTestRunner(t, 1)
	dir := t.TempDir()
	src := filepath.Join(dir, "loud.m4a")
	makeLoudTone(t, r, src, 5)

	render := func(volumeDB float64) float64 {
		out := filepath.Join(dir, fmt.Sprintf("bgm%.0f.m4a", volumeDB))
		if err := r.BuildBgmTrack(context.Background(), src, out, 5,
			BgmSpec{VolumeDB: volumeDB}); err != nil {
			t.Fatalf("BuildBgmTrack(%v) 失败: %v", volumeDB, err)
		}
		return integratedLUFS(t, out)
	}

	base := render(0)
	quieter := render(-6)

	// 偏移必须真的起作用，且方向正确。
	delta := base - quieter
	if delta < 4.5 || delta > 7.5 {
		t.Errorf("volume_db=-6 应让响度下降约 6 LU，实际下降 %.1f LU（%.1f -> %.1f）",
			delta, base, quieter)
	}
}

// TestMusicOnlyMixIsNotPulledToDialogueLoudness 是"默认 BGM 太响"的**回归测试**。
//
// 纯 BGM 的成片原来会被最后那道 `loudnorm=I=-16`（对白口径）拽到人声响度。
// 现在它应当停在配乐基准附近。
func TestMusicOnlyMixIsNotPulledToDialogueLoudness(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}
	r := newTestRunner(t, 1)
	dir := t.TempDir()
	src := filepath.Join(dir, "loud.m4a")
	bgm := filepath.Join(dir, "bgm.m4a")
	mixed := filepath.Join(dir, "mixed.m4a")
	makeLoudTone(t, r, src, 5)
	if err := r.BuildBgmTrack(context.Background(), src, bgm, 5, BgmSpec{}); err != nil {
		t.Fatalf("BuildBgmTrack 失败: %v", err)
	}
	// 旁白为空 = 纯配乐成片（用户关了 TTS、或只用背景音乐的情形）。
	if err := r.MixSoundtrack(context.Background(), "", bgm, mixed, 0); err != nil {
		t.Fatalf("MixSoundtrack 失败: %v", err)
	}

	got := integratedLUFS(t, mixed)
	if got > -18 {
		t.Errorf("纯配乐混音被拉到了 %.1f LUFS —— 对白口径(-16)会把背景音乐弄得过响；"+
			"应当停在配乐基准 %.0f 附近", got, bgmReferenceLUFS)
	}
}

// 对照组：**有旁白**时仍然要归一到对白口径，否则旁白会太小。
func TestVoiceMixStillNormalisesToDialogueLoudness(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}
	r := newTestRunner(t, 1)
	dir := t.TempDir()
	voice := filepath.Join(dir, "voice.m4a")
	bgm := filepath.Join(dir, "bgm.m4a")
	mixed := filepath.Join(dir, "mixed.m4a")
	makeEvalAudio(t, r, voice, "0.3*sin(2*PI*2000*t)", 5)
	makeLoudTone(t, r, bgm, 5)

	if err := r.MixSoundtrack(context.Background(), voice, bgm, mixed, 0); err != nil {
		t.Fatalf("MixSoundtrack 失败: %v", err)
	}
	got := integratedLUFS(t, mixed)
	if diff := got - defaultLoudnessLUFS; diff > 3 || diff < -3 {
		t.Errorf("有旁白时应归一到对白口径 %.0f LUFS，实际 %.1f LUFS",
			defaultLoudnessLUFS, got)
	}
}
