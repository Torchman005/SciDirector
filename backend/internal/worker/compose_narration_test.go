package worker

// 端到端验证「真实配音 → 整片配音轨 → 字幕按真实配音时长对齐」这条路径。
//
// 用**合成的正弦音**当配音（时长已知、可断言），因此不依赖任何 TTS 服务商 ——
// 这正是「与服务商无关的那一半」：换成 Edge TTS / Azure / OpenAI / 本地模型，
// 变的只是产生音频那一步，这条链路一行都不用改。
//
// 为什么必须端到端跑：这条路径串起了四个各自有单测的环节
// （探测配音时长 → 构建整轨 → 按真实时长排字幕 → 封装成片），
// 而它们**接起来**是否正确（路径能不能读到、时长有没有对齐、音轨有没有真的进去），
// 只有把真实文件喂进去才知道。整条链路坏掉时的表现是「成片少了一句字幕的位置」
// 或「最后一句字幕挂着不动」，都不会报错。

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"testing"
	"time"

	goredis "github.com/redis/go-redis/v9"

	"github.com/itJinYu/SciDirector/backend/internal/archive"
	"github.com/itJinYu/SciDirector/backend/internal/config"
	"github.com/itJinYu/SciDirector/backend/internal/domain"
	"github.com/itJinYu/SciDirector/backend/internal/media"
	"github.com/itJinYu/SciDirector/backend/internal/queue"
	"github.com/itJinYu/SciDirector/backend/internal/store"
)

func requireFFmpegBins(t *testing.T) {
	t.Helper()
	for _, bin := range []string{"ffmpeg", "ffprobe"} {
		if _, err := exec.LookPath(bin); err != nil {
			t.Skipf("未找到 %s，跳过真实媒体集成测试: %v", bin, err)
		}
	}
}

// makeClip 生成一段纯色视频片段（无音轨）。
//
// 刻意不给它音轨：合成路径应当自己补静音轨（那条逻辑同样需要被真实跑到）。
func makeClip(t *testing.T, path string, seconds float64, color string) {
	t.Helper()
	cmd := exec.Command("ffmpeg", "-hide_banner", "-nostdin", "-y",
		"-f", "lavfi", "-i", fmt.Sprintf("color=c=%s:s=320x240:r=10:d=%.3f", color, seconds),
		"-pix_fmt", "yuv420p", path)
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("生成测试片段失败: %v\n%s", err, tailStr(string(out), 400))
	}
}

func makeTone(t *testing.T, path string, seconds float64) {
	t.Helper()
	cmd := exec.Command("ffmpeg", "-hide_banner", "-nostdin", "-y",
		"-f", "lavfi", "-i", fmt.Sprintf("sine=frequency=440:duration=%.3f", seconds),
		"-ar", "48000", "-ac", "2", path)
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("生成测试配音失败: %v\n%s", err, tailStr(string(out), 400))
	}
}

func tailStr(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[len(s)-n:]
}

// srtCues 解析 SRT 里的 [start, end] 秒区间。
func srtCues(t *testing.T, path string) [][2]float64 {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("读字幕失败: %v", err)
	}
	re := regexp.MustCompile(`(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})`)
	var out [][2]float64
	for _, m := range re.FindAllStringSubmatch(string(raw), -1) {
		toSec := func(h, mnt, s, ms string) float64 {
			hi, _ := strconv.Atoi(h)
			mi, _ := strconv.Atoi(mnt)
			si, _ := strconv.Atoi(s)
			msi, _ := strconv.Atoi(ms)
			return float64(hi*3600+mi*60+si) + float64(msi)/1000
		}
		out = append(out, [2]float64{toSec(m[1], m[2], m[3], m[4]), toSec(m[5], m[6], m[7], m[8])})
	}
	return out
}

// TestComposeAlignsSubtitlesToRealNarration 是这条路径的端到端证明。
//
// 场景刻意做成「画面比旁白长」：3 秒的画面配 1.2 秒的旁白。
// 若字幕按窗口铺满（旧的估算行为），第一条字幕会一直挂到 3 秒 ——
// 声音早就停了、观众也早就读完了，字幕却还在，这是最典型的音画不同步。
func TestComposeAlignsSubtitlesToRealNarration(t *testing.T) {
	requireFFmpegBins(t)
	addr := requireRedis(t)
	ctx := context.Background()

	rdb := goredis.NewClient(&goredis.Options{Addr: addr, DB: testRedisDB})
	if err := rdb.FlushDB(ctx).Err(); err != nil {
		t.Fatalf("清空测试库失败: %v", err)
	}
	_ = rdb.Close()

	work := t.TempDir()
	redisCfg := config.RedisConfig{Addr: addr, DB: testRedisDB}
	st, err := store.New(ctx, redisCfg)
	if err != nil {
		t.Fatalf("构造 Store 失败: %v", err)
	}
	t.Cleanup(func() { _ = st.Close() })

	cfg := &config.Config{
		Env:      "test",
		LogLevel: "error",
		Redis:    redisCfg,
		Media: config.MediaConfig{
			FFmpegBin: "ffmpeg", FFprobeBin: "ffprobe",
			MaxParallel: 2, CommandTimeout: 2 * time.Minute,
			WorkDir: work,
			// 用小规格跑，避免 1080p 归一化把用例拖慢到几十秒。
			FPS: 10, Width: 320, Height: 240,
			SubtitleEnabled: true,
		},
		Pipeline: config.PipelineConfig{ShotMaxAttempts: 3, CriticScoreThreshold: 0.75},
		Archive:  config.ArchiveConfig{Backend: "none"},
	}
	runner, err := media.NewRunner(cfg.Media)
	if err != nil {
		t.Fatalf("构造 media Runner 失败: %v", err)
	}
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	proc := NewProcessor(cfg, st, nil, nil, runner, archive.NoopArchiver{}, logger)

	jobID := "job-narr-" + strconv.FormatInt(time.Now().UnixNano(), 36)
	jobDir := filepath.Join(work, jobID)
	if err := os.MkdirAll(jobDir, 0o755); err != nil {
		t.Fatalf("建工作目录失败: %v", err)
	}

	// 镜头 0：3 秒画面 + 1.2 秒旁白（画面明显比旁白长）
	// 镜头 1：2 秒画面 + 无旁白（回退到铺满窗口）
	clip0 := filepath.Join(jobDir, "shot_000.mp4")
	clip1 := filepath.Join(jobDir, "shot_001.mp4")
	makeClip(t, clip0, 3.0, "navy")
	makeClip(t, clip1, 2.0, "darkgreen")
	voice0 := filepath.Join(jobDir, "shot_000_voice.wav")
	makeTone(t, voice0, 1.2)

	job := &domain.Job{
		JobID:             jobID,
		RawScript:         "测试脚本内容需要足够长以通过校验，这里补充一些文字凑够长度。",
		TargetDurationSec: 30,
		Locale:            "zh-CN",
		Status:            domain.JobRendering,
		CreatedAt:         time.Now().UTC(),
		Shots: []*domain.Shot{
			{
				ShotID: domain.ShotID(jobID, 0), JobID: jobID, Index: 0,
				Tag: domain.TagAmbience, Engine: domain.EngineStock, DurationSec: 3,
				Narration: "第一句话。第二句话。", VisualBrief: "画面一",
				Status: domain.StatusApproved, Attempt: 1,
				Artifact: &domain.Artifact{
					ArtifactID: "a0", ShotID: domain.ShotID(jobID, 0),
					VideoPath: clip0, AudioPath: voice0, // ← 配音在这里
					DurationSec: 3, Width: 320, Height: 240, FPS: 10, Engine: "stock",
				},
			},
			{
				ShotID: domain.ShotID(jobID, 1), JobID: jobID, Index: 1,
				Tag: domain.TagAmbience, Engine: domain.EngineStock, DurationSec: 2,
				Narration: "第三个镜头的旁白。", VisualBrief: "画面二",
				Status: domain.StatusApproved, Attempt: 1,
				Artifact: &domain.Artifact{
					ArtifactID: "a1", ShotID: domain.ShotID(jobID, 1),
					VideoPath: clip1, DurationSec: 2, Width: 320, Height: 240, FPS: 10, Engine: "stock",
				},
			},
		},
	}
	if err := st.SaveJob(ctx, job); err != nil {
		t.Fatalf("写入任务失败: %v", err)
	}

	task := ComposeTask{
		Task:    nil,
		Payload: &queue.ComposeJobPayload{JobID: jobID},
	}
	if err := proc.HandleComposeJob(ctx, task); err != nil {
		t.Fatalf("合成失败: %v", err)
	}

	// ---- 断言 1：成片存在、有音轨、时长等于两段画面之和 ----
	finalPath := filepath.Join(jobDir, "final.mp4")
	if _, err := os.Stat(finalPath); err != nil {
		t.Fatalf("成片不存在: %v", err)
	}
	probe, err := runner.Probe(ctx, finalPath)
	if err != nil {
		t.Fatalf("探测成片失败: %v", err)
	}
	if !probe.HasAudio {
		t.Error("成片没有音轨 —— 配音轨没有被封装进去（或封装时被丢了）")
	}
	if diff := probe.DurationSec - 5.0; diff > 0.4 || diff < -0.4 {
		t.Errorf("成片时长期望约 5.0s（3+2），实际 %.3f", probe.DurationSec)
	}

	// ---- 断言 2：字幕按**真实配音时长**对齐，而不是铺满画面窗口 ----
	srtPath := filepath.Join(jobDir, "final.srt")
	cues := srtCues(t, srtPath)
	if len(cues) == 0 {
		t.Fatalf("没有生成字幕: %s", srtPath)
	}

	var firstShotCues [][2]float64
	for _, c := range cues {
		if c[0] < 3.0 {
			firstShotCues = append(firstShotCues, c)
		}
	}
	if len(firstShotCues) == 0 {
		t.Fatalf("镜头 0 没有字幕，实际全部字幕：%v", cues)
	}
	lastEnd := firstShotCues[len(firstShotCues)-1][1]
	if lastEnd > 1.35 {
		t.Errorf("镜头 0 的旁白只有 1.2s，最后一条字幕却结束于 %.3fs —— "+
			"说明仍在按画面窗口（3s）铺满，末尾 1.8 秒是「声音停了字幕还挂着」", lastEnd)
	}
	if lastEnd < 1.0 {
		t.Errorf("镜头 0 的字幕结束过早（%.3fs），1.2 秒的旁白应当被完整覆盖", lastEnd)
	}

	// ---- 断言 3：没有配音的镜头回退成铺满窗口（不能被新逻辑误伤） ----
	var secondShotCues [][2]float64
	for _, c := range cues {
		if c[0] >= 2.9 {
			secondShotCues = append(secondShotCues, c)
		}
	}
	if len(secondShotCues) == 0 {
		t.Errorf("镜头 1 没有字幕，实际全部字幕：%v", cues)
	} else if end := secondShotCues[len(secondShotCues)-1][1]; end < 4.5 {
		t.Errorf("镜头 1 没有配音，应当回退成铺满窗口（约 5.0s），实际结束于 %.3fs"+
			" —— 没有配音的镜头不该被按配音时长截断", end)
	}

}
