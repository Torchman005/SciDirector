package httpapi

import (
	"strings"
	"testing"

	"github.com/itJinYu/SciDirector/backend/internal/media"
)

// TestValidateStyleGuideRejectsUnknownBackground 钉住"非法背景样式必须在入口被拒"。
//
// 这条守的是一个很容易被当成"小事"的缺陷：Python 侧对未知背景样式是**抛异常**的，
// 而 /generate 原先不校验它 —— 于是任务会先返回 202、落库、起队列，
// 等导演节点解析风格时才失败。用户看到的是一条"提交成功然后失败"的任务，
// 真正的原因（样式名拼错）埋在一句 Python 异常里。
func TestValidateStyleGuideRejectsUnknownBackground(t *testing.T) {
	cases := []struct {
		name string
		sg   map[string]any
		ok   bool
	}{
		{"缺省（没写这一项）", map[string]any{"preset": "tech"}, true},
		{"空对象", map[string]any{}, true},
		{"nil", nil, true},
		{"空串按缺省处理", map[string]any{"background_style": ""}, true},
		{"大写也要认", map[string]any{"background_style": "GRID"}, true},
		{"两侧空格要认", map[string]any{"background_style": "  grid  "}, true},
		// 反向对照：把**配色**的名字当背景样式传，必须被拒 ——
		// 这两张表容易混（都叫"风格"），而混了以后错误信息必须能指路。
		{"把配色名当样式传", map[string]any{"background_style": "tech"}, false},
		{"完全不存在的名字", map[string]any{"background_style": "扫描线"}, false},
		{"类型不对（数字）", map[string]any{"background_style": 42}, false},
	}

	for _, c := range cases {
		err := validateStyleGuide(c.sg)
		if c.ok && err != nil {
			t.Errorf("%s：不该报错，实际 %v", c.name, err)
		}
		if !c.ok && err == nil {
			t.Errorf("%s：应当被拒绝", c.name)
		}
	}
}

// TestValidateStyleGuideListsValidIDs 要求错误信息里带上可选值。
//
// 只说"非法"而不给可用清单，用户只能去翻文档或源码 ——
// 而这条错误信息本来就有能力把答案直接给出来。
func TestValidateStyleGuideListsValidIDs(t *testing.T) {
	err := validateStyleGuide(map[string]any{"background_style": "nope"})
	if err == nil {
		t.Fatal("应当报错")
	}
	msg := err.Error()
	for _, id := range media.BackgroundStyleIDs() {
		if !strings.Contains(msg, id) {
			t.Errorf("错误信息应当列出可用样式 %q，实际：%s", id, msg)
		}
	}
}

// TestAllBackgroundStyleIDsPassValidation 保证"表里的每一个都真的能用"。
//
// 这条防的是两边表不同步：Go 认、Python 不认（或反过来）时，
// 用户选了一个界面提供的选项却渲染失败。
func TestAllBackgroundStyleIDsPassValidation(t *testing.T) {
	for _, id := range media.BackgroundStyleIDs() {
		if err := validateStyleGuide(map[string]any{"background_style": id}); err != nil {
			t.Errorf("在册的样式 %q 竟然没通过校验: %v", id, err)
		}
	}
}
