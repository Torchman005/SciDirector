package httpapi

import (
	"fmt"
	"strings"

	"github.com/itJinYu/SciDirector/backend/internal/media"
)

// validateStyleGuide 校验 style_guide 里**由 Go 侧负责**的那些字段。
//
// 目前只有 `background_style`。为什么归 Go 管：这张 id 表在 Go 侧有权威实现
// （`media.IsBackgroundStyle`，与 Python 的 `backgrounds.py` 有测试互相钉住），
// 而 Python 拿到未知 id 是**抛异常**的。
//
// 关键在于"抛在哪里"：如果不在入口拦，`/generate` 会先返回 202、把任务落库、
// 起队列，等导演节点解析风格时才炸 —— 用户看到的是一条"提交成功然后失败"的任务，
// 而真正的原因（拼错了一个背景样式名）在一条 Python 异常里。
// 本地、确定、便宜的判断就该在本地做掉。
//
// `preset` 与两个色值**不在这里校验**：前者由 Python 的 STYLE_PRESETS 决定，
// Go 侧没有权威表可依据（照抄一份必然漂移）；后者是自由格式，无法穷举。
// 与其在这里编一套半吊子规则，不如让它们留在各自的真源处 ——
// 但要清楚，那两条路径确实会"先受理、后失败"。
//
// 这里刻意**不复用 stringField**：那个函数对非字符串一律返回空串，
// 而空串会被归一成合法的 `auto` —— 于是 `{"background_style": 42}`
// 这种类型错误会被**静默吞成默认值**，直到 Python 那边才炸
// （`(42 or "").strip()` 直接 AttributeError）。校验函数必须能区分
// "没填"、"填了空"、"填错了类型"这三件事。
func validateStyleGuide(sg map[string]any) error {
	if sg == nil {
		return nil
	}
	raw, present := sg["background_style"]
	// JSON 的 null 与"字段不存在"等价：都表示没设置。
	if !present || raw == nil {
		return nil
	}
	s, ok := raw.(string)
	if !ok {
		return fmt.Errorf("background_style 必须是字符串，实际是 %T", raw)
	}
	if !media.IsBackgroundStyle(s) {
		return fmt.Errorf("未知的背景样式 %q；可用：%s",
			s, strings.Join(media.BackgroundStyleIDs(), ", "))
	}
	return nil
}
