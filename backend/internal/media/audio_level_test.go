package media

import (
	"context"
	"path/filepath"
	"testing"
)

func TestParseVolumeDetect(t *testing.T) {
	// 真实 ffmpeg 的 volumedetect 输出片段（含前置的流信息噪声）。
	out := `[Parsed_volumedetect_0 @ 0x55] n_samples: 240000
[Parsed_volumedetect_0 @ 0x55] mean_volume: -18.6 dB
[Parsed_volumedetect_0 @ 0x55] max_volume: -3.2 dB
[Parsed_volumedetect_0 @ 0x55] histogram_0db: 0
`
	got, err := parseVolumeDetect(out)
	if err != nil {
		t.Fatalf("解析失败: %v", err)
	}
	if got.MeanDBFS != -18.6 || got.PeakDBFS != -3.2 {
		t.Errorf("解析结果不对：%+v", got)
	}
}

func TestParseVolumeDetectRejectsGarbage(t *testing.T) {
	// 解析失败必须**报错**而不是返回 0：返回 0 会让界面显示 "0 dB"，
	// 看起来还挺响亮，而真相是"根本没测到" —— 这是最难发现的一类错误。
	if _, err := parseVolumeDetect("no volume info here"); err == nil {
		t.Fatal("解析不到电平时报错，而不是返回 0")
	}
}

func TestProbeAudioLevelMeasuresRealFile(t *testing.T) {
	requireFFmpeg(t)
	if testing.Short() {
		t.Skip("短模式跳过真实媒体集成测试")
	}
	r := newTestRunner(t, 1)
	dir := t.TempDir()

	// 造一段已知很响的音频（振幅 0.7 ≈ -3 dBFS 峰值）。
	loud := filepath.Join(dir, "loud.m4a")
	makeEvalAudio(t, r, loud, "0.7*sin(2*PI*440*t)", 3)
	lv, err := r.ProbeAudioLevel(context.Background(), loud)
	if err != nil {
		t.Fatalf("ProbeAudioLevel 失败: %v", err)
	}
	// 峰值应当接近 0.7 的满度换算值（20*log10(0.7) ≈ -3.1 dB）。
	if lv.PeakDBFS < -6 || lv.PeakDBFS > -1 {
		t.Errorf("峰值应在 -3 dB 附近，实际 %.1f", lv.PeakDBFS)
	}
	if lv.MeanDBFS > lv.PeakDBFS {
		t.Errorf("平均电平不该高于峰值：mean=%.1f peak=%.1f", lv.MeanDBFS, lv.PeakDBFS)
	}

	// 对照：把同一段音频压低 20 dB，测出的平均电平必须明显更低。
	quiet := filepath.Join(dir, "quiet.m4a")
	if err := r.run(context.Background(),
		"-hide_banner", "-nostdin", "-y", "-i", loud, "-af", "volume=-20dB", quiet); err != nil {
		t.Fatalf("生成低电平素材失败: %v", err)
	}
	lvQuiet, err := r.ProbeAudioLevel(context.Background(), quiet)
	if err != nil {
		t.Fatalf("ProbeAudioLevel 失败: %v", err)
	}
	if diff := lv.MeanDBFS - lvQuiet.MeanDBFS; diff < 15 || diff > 25 {
		t.Errorf("压低 20dB 后平均电平应下降约 20dB，实际下降 %.1f（%.1f -> %.1f）",
			diff, lv.MeanDBFS, lvQuiet.MeanDBFS)
	}
}
