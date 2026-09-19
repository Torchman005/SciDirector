package media

// 读取配音的**句级时间戳 sidecar**。
//
// ## 这是什么
//
// TTS 服务商（如 Edge TTS）在合成时会顺带给出每句话在音频里的起止时间。
// Python 侧把它写成音频旁边的 JSON：
//
//	<音频文件>.marks.json     例：narration.mp3.marks.json
//
// 形如：
//
//	{"version":1,"provider":"edge","duration_sec":3.7625,
//	 "marks":[{"text":"…","start_sec":0.1,"duration_sec":3.6625}]}
//
// ## 为什么走 sidecar 而不是加 proto 字段
//
// 与 `payload_json` 当初的取舍一致：这个结构还在演进，走 JSON 不必每次重新生成
// 两侧代码；而且它是**可选增强** —— 读不到就回退到「按镜头真实音频时长对齐」，
// 链路不会因为某家服务商不给时间戳而断掉。
//
// ## 读取策略：尽力而为，永不报错
//
// 这个文件是**增强信息**：缺了、坏了、版本不认识，都不该让合成失败。
// 因此 `ReadNarrationMarks` 只返回 `[]NarrationMark`（读不到就是 nil），
// 把「没有时间戳」与「有时间戳」收敛成同一个调用形态 ——
// 调用方不需要为「这家服务商不给时间戳」写分支。
//
// 但要**留痕**：读到了却解析失败（说明格式对不上）与文件不存在是两回事，
// 前者是我们自己的契约出了问题，值得一条 warn。

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

// NarrationMarkFormatVersion 必须与 Python 侧 `tts.base.MARKS_FORMAT_VERSION` 一致。
//
// 版本不认识时**忽略**而不是猜着解析：宁可回退到按时长对齐，
// 也不要读出半截错位的字幕时间。
const NarrationMarkFormatVersion = 1

// NarrationMark 是一句话在配音音频里的位置。
type NarrationMark struct {
	Text        string
	StartSec    float64
	DurationSec float64
}

// EndSec 返回该句的结束时间。
func (m NarrationMark) EndSec() float64 { return m.StartSec + m.DurationSec }

// narrationMarksFile 是 sidecar 的命名约定，与 Python 侧 `marks_sidecar_path` 对应。
func narrationMarksFile(audioPath string) string {
	return audioPath + ".marks.json"
}

// narrationMarksPayload 是 sidecar 的 JSON 结构（与 Python 侧字段名一一对应）。
type narrationMarksPayload struct {
	Version     int     `json:"version"`
	Provider    string  `json:"provider"`
	DurationSec float64 `json:"duration_sec"`
	Marks       []struct {
		Text        string  `json:"text"`
		StartSec    float64 `json:"start_sec"`
		DurationSec float64 `json:"duration_sec"`
	} `json:"marks"`
}

// ReadNarrationMarks 读取配音的句级时间戳。
//
// 返回值语义（刻意把三种情形分开，避免「静默降级」）：
//
//	marks=nil, err=nil  → 没有 sidecar。**正常**：该服务商不给时间戳，或还没接 TTS。
//	marks=nil, err!=nil → sidecar 在，但用不了（解析失败 / 版本不认识 / 读不动）。
//	                      这是**我们两侧契约对不上**的信号，调用方应当记一条 warn。
//	marks,     err=nil  → 可用。
//
// 为什么不在这里直接打日志：media 包是纯库、**不持有 logger**
// （`Runner` 就没有 logger 字段，日志一律由调用方记）。
// 为了打一条 warn 就把 logger 塞进整个包，得不偿失。
func ReadNarrationMarks(audioPath string) ([]NarrationMark, error) {
	if audioPath == "" {
		return nil, nil
	}
	path := narrationMarksFile(audioPath)
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil // 没有 sidecar 是正常情形
		}
		return nil, fmt.Errorf("media: 读取配音时间戳失败 %s: %w", path, err)
	}

	var payload narrationMarksPayload
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, fmt.Errorf("media: 配音时间戳不是合法 JSON %s: %w", path, err)
	}
	if payload.Version != NarrationMarkFormatVersion {
		return nil, fmt.Errorf(
			"media: 配音时间戳版本不认识 %s（got %d, want %d）—— 两侧契约可能已经对不上",
			path, payload.Version, NarrationMarkFormatVersion)
	}

	out := make([]NarrationMark, 0, len(payload.Marks))
	for _, m := range payload.Marks {
		if m.DurationSec <= 0 || m.StartSec < 0 {
			// 单条坏记录只跳过它，不让整份时间戳作废。
			continue
		}
		out = append(out, NarrationMark{
			Text: m.Text, StartSec: m.StartSec, DurationSec: m.DurationSec,
		})
	}
	if len(out) == 0 {
		return nil, nil // 有文件但一条可用记录都没有：等同于没有时间戳
	}
	return out, nil
}

// NarrationMarksPath 暴露 sidecar 路径约定，供测试与排查使用。
func NarrationMarksPath(audioPath string) string {
	return filepath.Clean(narrationMarksFile(audioPath))
}
