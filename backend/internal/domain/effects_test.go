package domain

import (
	"testing"
)

// ptr 返回一个 bool 指针 —— Loop 用指针是为了区分「没填」与「显式 false」。
func ptr(v bool) *bool { return &v }

func TestEffectsValidateAcceptsDefaults(t *testing.T) {
	// 全零值（"什么都没配"）必须合法：绝大多数任务本来就不带后期效果。
	if err := (Effects{}).Validate(); err != nil {
		t.Fatalf("空 effects 应当合法，实际: %v", err)
	}
}

func TestEffectsValidateRejectsOutOfRange(t *testing.T) {
	cases := map[string]Effects{
		"grade_strength 过大": {GradeStrength: 1.5},
		"grade_strength 为负": {GradeStrength: -0.1},
		"fade_in 过大":        {FadeInSec: 30},
		"fade_out 为负":       {FadeOutSec: -1},
		"响度过高":              {LoudnessLUFS: 3},
		"响度过低":              {LoudnessLUFS: -80},
		"配乐音量过大":            {BGM: &BGMEffects{VolumeDB: 40}},
		"配乐淡入为负":            {BGM: &BGMEffects{FadeInSec: -1}},
		"asset_id 过长":       {BGM: &BGMEffects{AssetID: string(make([]byte, 200))}},
	}
	for name, e := range cases {
		if err := e.Validate(); err == nil {
			t.Errorf("%s：应当被拒绝", name)
		}
	}
}

func TestEffectsValidateAcceptsBoundaryValues(t *testing.T) {
	// 边界值本身必须合法，否则"配到上限"会被莫名拒绝。
	e := Effects{
		GradeStrength: MaxGradeStrength,
		FadeInSec:     MaxFadeSec,
		FadeOutSec:    MaxFadeSec,
		LoudnessLUFS:  MaxLoudnessLUFS,
		BGM:           &BGMEffects{VolumeDB: MaxBGMVolumeDB, FadeOutSec: MaxBGMFadeSec},
	}
	if err := e.Validate(); err != nil {
		t.Fatalf("边界值应当合法，实际: %v", err)
	}
}

func TestEffectsHasBGMRequiresResolvedPath(t *testing.T) {
	// 判据是**服务端解析后的 Path**，而不是 AssetID。
	// 只填了 AssetID 却没解析出路径，说明素材不存在 ——
	// 此时必须什么都不做，而不是让合成阶段去打开一个空路径。
	if (Effects{BGM: &BGMEffects{AssetID: "abc"}}).HasBGM() {
		t.Error("只有 asset_id、没有 resolved path 时不该认为配了 BGM")
	}
	if !(Effects{BGM: &BGMEffects{AssetID: "abc", Path: "/x/y.mp3"}}).HasBGM() {
		t.Error("有 resolved path 时应当认为配了 BGM")
	}
	if (Effects{}).HasBGM() {
		t.Error("没有 BGM 时当然不该认为配了")
	}
}

func TestBGMLoopDefaultsToTrue(t *testing.T) {
	// 缺省循环是刻意的：短片当 BGM 是常态，不循环会让后半段静音，
	// 而"后半段没声音"不会报错，观众只会觉得配乐没了。
	var nilBGM *BGMEffects
	if !nilBGM.LoopOrDefault() {
		t.Error("BGM 为 nil 时 LoopOrDefault 应当为 true")
	}
	if !(&BGMEffects{}).LoopOrDefault() {
		t.Error("未显式设置时应当默认循环")
	}
	if (&BGMEffects{Loop: ptr(false)}).LoopOrDefault() {
		t.Error("显式 false 必须被尊重 —— 用指针就是为了区分它与「没填」")
	}
}

func TestSubtitleStyleRequiresBurning(t *testing.T) {
	// 设了字幕样式却没开烧录 -> **报错而不是忽略**。
	// 软字幕的样式由播放器决定、容器里存不下，所以"给未烧录的字幕配样式"
	// 在技术上必然无效；静默忽略会让用户看到"我配了字号却完全没变化"，
	// 而没有任何线索指向真正的原因。
	e := Effects{SubtitleStyle: &SubtitleStyle{FontSize: 48}}
	if err := e.Validate(); err == nil {
		t.Fatal("设了 subtitle_style 但没开 burn_subtitles 时应当报错")
	}
	e.BurnSubtitles = true
	if err := e.Validate(); err != nil {
		t.Fatalf("同时开启烧录后应当合法，实际: %v", err)
	}
}

func TestSubtitleStyleValidateRejectsOutOfRange(t *testing.T) {
	cases := map[string]SubtitleStyle{
		"字号过大":    {FontSize: 500},
		"字号为负":    {FontSize: -1},
		"描边过宽":    {OutlineWidth: 20},
		"底边距过大":   {MarginV: 5000},
		"颜色缺 #":   {PrimaryColor: "FF0000"},
		"颜色位数不对":  {PrimaryColor: "#FFF"},
		"颜色非十六进制": {PrimaryColor: "#ZZZZZZ"},
	}
	for name, s := range cases {
		e := Effects{BurnSubtitles: true, SubtitleStyle: &s}
		if err := e.Validate(); err == nil {
			t.Errorf("%s：应当被拒绝", name)
		}
	}
}

func TestSubtitleStyleAcceptsOmittedFields(t *testing.T) {
	// 全部留空 = "全自动"，必须合法：那是最常见的用法。
	e := Effects{BurnSubtitles: true, SubtitleStyle: &SubtitleStyle{}}
	if err := e.Validate(); err != nil {
		t.Fatalf("空字幕样式应当合法（表示全自动），实际: %v", err)
	}
}
