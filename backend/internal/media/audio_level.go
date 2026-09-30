package media

import (
	"bytes"
	"context"
	"fmt"
	"regexp"
	"strconv"
)

// AudioLevel 是一段音频的实测电平。
//
// 为什么要在**上传时**就测出来：用户的原话是"上传以后无法判断 BGM 的音量"。
// 音乐文件的响度差异极大（实测常见 -3 到 -25 dBFS），而配乐音量滑块是**相对**
// 基准的偏移 —— 不知道文件本身多响，滑块该往哪边拖就只能靠猜。
//
// 顺带它也解释了"为什么同一档音量，这首歌吵那首轻"：不是滑块坏了，
// 是两个文件的起点差了 20 dB。
type AudioLevel struct {
	// MeanDBFS 是平均电平（dBFS，越接近 0 越响）。
	MeanDBFS float64
	// PeakDBFS 是峰值电平。接近 0 说明文件可能已经削顶（爆音），
	// 那是**源文件的问题**，再怎么调音量也救不回来。
	PeakDBFS float64
}

var (
	volumeDetectMeanRe = regexp.MustCompile(`mean_volume:\s*(-?[\d.]+)\s*dB`)
	volumeDetectPeakRe = regexp.MustCompile(`max_volume:\s*(-?[\d.]+)\s*dB`)
)

// parseVolumeDetect 从 ffmpeg volumedetect 的输出里解析电平。
//
// 抽成纯函数是为了能脱离进程单测：解析这类文本输出最容易在
// "格式微调"时静默失效（正则不匹配 -> 返回 0 -> 界面上显示 "0 dB"，
// 看起来还挺响亮），而那属于最难发现的一类错误。
func parseVolumeDetect(output string) (AudioLevel, error) {
	mean := volumeDetectMeanRe.FindStringSubmatch(output)
	peak := volumeDetectPeakRe.FindStringSubmatch(output)
	if mean == nil || peak == nil {
		return AudioLevel{}, fmt.Errorf("media: 未能从 volumedetect 输出里解析出电平")
	}
	m, err := strconv.ParseFloat(mean[1], 64)
	if err != nil {
		return AudioLevel{}, fmt.Errorf("media: 解析 mean_volume %q 失败: %w", mean[1], err)
	}
	p, err := strconv.ParseFloat(peak[1], 64)
	if err != nil {
		return AudioLevel{}, fmt.Errorf("media: 解析 max_volume %q 失败: %w", peak[1], err)
	}
	return AudioLevel{MeanDBFS: m, PeakDBFS: p}, nil
}

// ProbeAudioLevel 测一段音频的平均与峰值电平。
//
// 与 Probe / ProbeAudio 一样计入全局并发闸门：它虽然轻，但同样会读整个文件，
// 对一个几十 MB 的音频来说并非零成本；绕过闸门就等于给"并发上传时的资源放大"
// 留了后门。
func (r *Runner) ProbeAudioLevel(ctx context.Context, path string) (AudioLevel, error) {
	release, err := r.acquire(ctx)
	if err != nil {
		return AudioLevel{}, err
	}
	defer release()

	// `-f null -` 表示只做分析、不产出文件；volumedetect 的统计写在 stderr。
	cmd, cancel := r.newCmd(ctx, r.ffmpegBin,
		"-hide_banner", "-nostdin", "-i", path,
		"-af", "volumedetect", "-f", "null", "-",
	)
	defer cancel()

	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if runErr := cmd.Run(); runErr != nil {
		if ctx.Err() != nil {
			return AudioLevel{}, fmt.Errorf("media: 电平探测被取消: %w", ctx.Err())
		}
		return AudioLevel{}, fmt.Errorf("media: 电平探测失败: %w\nstderr: %s",
			runErr, tail(stderr.String(), 800))
	}
	return parseVolumeDetect(stderr.String())
}
