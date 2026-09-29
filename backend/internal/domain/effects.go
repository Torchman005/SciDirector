package domain

import (
	"fmt"
	"math"
	"strings"
)

// Effects 是**后期效果**配置：配乐、调色、淡入淡出、响度。
//
// 与 StyleGuide 的分工是刻意的，两者不要混：
//
//	StyleGuide -> 影响**生成**（配色、字体、风格预设），会进提示词；
//	Effects    -> 影响**后期**（合成阶段），只由 Go 的媒体层消费。
//
// 分开的理由：调色与配乐发生在"画面已经渲染完"之后，把它们塞进 style_guide
// 会让"这份配置到底影响哪一段"变得不可推理 —— 而本项目已经吃过一次
// 「配置项写对了却没人读」的亏。
type Effects struct {
	// BGM 为空表示不加背景音乐。
	BGM *BGMEffects `json:"bgm,omitempty"`
	// Grade 是调色方案名（none/warm/cool/high_contrast/film）。
	// 空串按 none 处理。**合法性由 httpapi 层用 media.PlanGrade 校验** ——
	// 方案表属于媒体层，domain 不该反过来依赖它。
	Grade         string  `json:"grade,omitempty"`
	GradeStrength float64 `json:"grade_strength,omitempty"`
	// FadeInSec / FadeOutSec 是片头片尾淡入淡出（秒）。
	FadeInSec  float64 `json:"fade_in_sec,omitempty"`
	FadeOutSec float64 `json:"fade_out_sec,omitempty"`
	// BurnSubtitles 为 true 时把字幕烧进画面（必须重编码）。
	// 缺省 false：软字幕不损画质，且播放器可以关掉。
	BurnSubtitles bool `json:"burn_subtitles,omitempty"`
	// LoudnessLUFS 是混音后的整体响度目标，0 表示用媒体层缺省值。
	LoudnessLUFS float64 `json:"loudness_lufs,omitempty"`
}

// BGMEffects 是背景音乐配置。
type BGMEffects struct {
	// AssetID 指向上传的素材（见 assets 包）。**请求里只传它**。
	AssetID string `json:"asset_id,omitempty"`
	// Path 是服务端解析出来的绝对路径，**不接受来自请求**。
	//
	// 这样设计是为了不把"读服务端任意文件"变成一个 HTTP 接口：
	// 请求方只能引用自己上传过的素材，路径由服务端自己拼。
	Path string `json:"path,omitempty"`
	// VolumeDB 是配乐相对电平（dB）。0 表示不额外衰减（由响度归一化兜底）。
	VolumeDB float64 `json:"volume_db,omitempty"`
	// Loop 用指针是为了区分「没填」与「显式 false」。
	// 缺省语义是**循环**：短片当 BGM 是常态，不循环会让后半段静音，
	// 而"后半段没声音"不会报错，观众只会觉得配乐没了。
	Loop *bool `json:"loop,omitempty"`
	// FadeInSec / FadeOutSec 是配乐自身的淡入淡出。
	FadeInSec  float64 `json:"fade_in_sec,omitempty"`
	FadeOutSec float64 `json:"fade_out_sec,omitempty"`
}

// LoopOrDefault 返回是否循环，未显式设置时按 true。
func (b *BGMEffects) LoopOrDefault() bool {
	if b == nil || b.Loop == nil {
		return true
	}
	return *b.Loop
}

// HasBGM 报告是否真的配了背景音乐。
//
// 判据是 **Path 非空**（服务端解析后的结果），而不是 AssetID：
// 只填了 AssetID 却没解析出路径，说明素材不存在，此时应当什么都不做，
// 而不是让合成阶段去打开一个空路径。
func (e Effects) HasBGM() bool {
	return e.BGM != nil && strings.TrimSpace(e.BGM.Path) != ""
}

// 各项效果的取值范围。
//
// 上限不是随便定的：淡入淡出比整片还长时，ffmpeg 不报错但效果诡异
// （一片从头淡到尾），所以在入口就拦住。
const (
	MaxFadeSec          = 10.0
	MinLoudnessLUFS     = -40.0
	MaxLoudnessLUFS     = -5.0
	MinBGMVolumeDB      = -60.0
	MaxBGMVolumeDB      = 12.0
	MinGradeStrength    = 0.0
	MaxGradeStrength    = 1.0
	MaxBGMFadeSec       = 10.0
	MaxBGMAssetIDLength = 128
)

// Validate 校验后期效果配置的**取值范围**。
//
// 只管范围，不管"grade 这个名字认不认识"：方案表在媒体层，
// 由 httpapi 调用 media.PlanGrade 校验。这样 domain 不必反向依赖 media。
func (e Effects) Validate() error {
	if e.GradeStrength < MinGradeStrength || e.GradeStrength > MaxGradeStrength {
		return fmt.Errorf("domain: grade_strength 应在 [%.1f, %.1f] 之间，实际 %.3f",
			MinGradeStrength, MaxGradeStrength, e.GradeStrength)
	}
	if e.FadeInSec < 0 || e.FadeInSec > MaxFadeSec {
		return fmt.Errorf("domain: fade_in_sec 应在 [0, %.1f] 之间，实际 %.3f", MaxFadeSec, e.FadeInSec)
	}
	if e.FadeOutSec < 0 || e.FadeOutSec > MaxFadeSec {
		return fmt.Errorf("domain: fade_out_sec 应在 [0, %.1f] 之间，实际 %.3f", MaxFadeSec, e.FadeOutSec)
	}
	if e.LoudnessLUFS != 0 && (e.LoudnessLUFS < MinLoudnessLUFS || e.LoudnessLUFS > MaxLoudnessLUFS) {
		return fmt.Errorf("domain: loudness_lufs 应在 [%.0f, %.0f] 之间（0 表示用缺省值），实际 %.1f",
			MinLoudnessLUFS, MaxLoudnessLUFS, e.LoudnessLUFS)
	}
	if b := e.BGM; b != nil {
		if len(b.AssetID) > MaxBGMAssetIDLength {
			return fmt.Errorf("domain: bgm.asset_id 过长（上限 %d）", MaxBGMAssetIDLength)
		}
		if math.IsNaN(b.VolumeDB) || b.VolumeDB < MinBGMVolumeDB || b.VolumeDB > MaxBGMVolumeDB {
			return fmt.Errorf("domain: bgm.volume_db 应在 [%.0f, %.0f] 之间，实际 %.1f",
				MinBGMVolumeDB, MaxBGMVolumeDB, b.VolumeDB)
		}
		for name, v := range map[string]float64{"bgm.fade_in_sec": b.FadeInSec, "bgm.fade_out_sec": b.FadeOutSec} {
			if v < 0 || v > MaxBGMFadeSec {
				return fmt.Errorf("domain: %s 应在 [0, %.1f] 之间，实际 %.3f", name, MaxBGMFadeSec, v)
			}
		}
	}
	return nil
}
