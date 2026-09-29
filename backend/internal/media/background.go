// 背景样式预设与效果预览用的测试卡。
//
// 背景样式的**唯一真源在 Python**（`scidirector_ai/backgrounds.py`）：它给出
// 提示词描述与 CSS 片段，决定模型真的画出什么。这里的表只服务于一件事 ——
// **效果预览要有背景可看**，所以用 ffmpeg 把同一个预设画出来。
//
// 两边的 id 必须一致，由 Python 侧的 `test_backgrounds.py` 指名钉住
// （与本项目处理引擎映射的惯例相同：两边各写一份 + 一条测试点名对侧）。
// 少了这条约束，加预设时只改一边，表现就是"前端能选、预览却报错"。
package media

import (
	"fmt"
	"sort"
	"strconv"
	"strings"
)

// backgroundStyleIDs 是全部合法的背景样式 id（顺序与 Python 侧一致）。
var backgroundStyleIDs = []string{
	"auto",
	"solid",
	"gradient",
	"grid",
	"vignette",
	"noise",
	"scanlines",
}

// BackgroundStyleIDs 返回全部背景样式 id（副本，调用方改不动内部状态）。
func BackgroundStyleIDs() []string {
	out := make([]string, len(backgroundStyleIDs))
	copy(out, backgroundStyleIDs)
	return out
}

// IsBackgroundStyle 报告 id 是否在册。
func IsBackgroundStyle(id string) bool {
	n := normalizeBackgroundStyle(id)
	for _, v := range backgroundStyleIDs {
		if v == n {
			return true
		}
	}
	return false
}

// normalizeBackgroundStyle 把 id 归一成小写并处理缺省值。
func normalizeBackgroundStyle(id string) string {
	n := strings.ToLower(strings.TrimSpace(id))
	if n == "" {
		return "auto"
	}
	return n
}

// parseHexColour 把 `#RRGGBB` 转成 ffmpeg 认的 `0xRRGGBB`。
//
// 刻意只接受带 `#` 的六位写法：与字幕样式的颜色走同一套严格规则，
// 免得出现"接口收了一种、渲染器认另一种"的漂移。
func parseHexColour(hex string) (string, error) {
	h := strings.TrimSpace(hex)
	if !strings.HasPrefix(h, "#") || len(h) != 7 {
		return "", fmt.Errorf("media: 颜色必须是 #RRGGBB 形式，实际 %q", hex)
	}
	if _, err := strconv.ParseUint(h[1:], 16, 32); err != nil {
		return "", fmt.Errorf("media: 颜色含非法十六进制字符：%q", hex)
	}
	return "0x" + strings.ToUpper(h[1:]), nil
}

// blendHexColour 按比例混合两个 `0xRRGGBB` 颜色，t=0 取 a、t=1 取 b。
//
// 为什么需要混合：`gradients` 等滤镜的颜色**不支持 alpha**，而设计上想要的是
// "背景色到主色的一点点过渡"。直接拿主色当终点会得到一条刺眼的渐变，
// 所以先把主色按比例混进背景色，再交给滤镜。
func blendHexColour(a, b string, t float64) (string, error) {
	if t < 0 {
		t = 0
	}
	if t > 1 {
		t = 1
	}
	ca, err := parseHexColour("#" + strings.TrimPrefix(strings.ToUpper(a), "0X"))
	if err != nil {
		return "", err
	}
	cb, err := parseHexColour("#" + strings.TrimPrefix(strings.ToUpper(b), "0X"))
	if err != nil {
		return "", err
	}
	ar, ag, ab := hexParts(ca)
	br, bg, bb := hexParts(cb)
	mix := func(x, y int) int {
		return int(float64(x) + (float64(y)-float64(x))*t + 0.5)
	}
	return fmt.Sprintf("0x%02X%02X%02X", mix(ar, br), mix(ag, bg), mix(ab, bb)), nil
}

// hexParts 把 `0xRRGGBB` 拆成三个分量。
func hexParts(c string) (int, int, int) {
	v, _ := strconv.ParseUint(strings.TrimPrefix(strings.ToUpper(c), "0X"), 16, 32)
	return int(v >> 16 & 0xFF), int(v >> 8 & 0xFF), int(v & 0xFF)
}

// BackgroundFilter 构造把背景画出来的 lavfi 滤镜串。
//
// 尺寸相关的间距都按画面宽高**按比例**算，因此同一套预设在任何分辨率下
// 观感一致 —— 预览与实际成片的分辨率不同时也不会看起来是两种背景。
func BackgroundFilter(styleID, backgroundColour, primaryColour string, w, h, fps int) (string, error) {
	if w <= 0 || h <= 0 || fps <= 0 {
		return "", fmt.Errorf("media: 背景尺寸/帧率非法（%dx%d@%d）", w, h, fps)
	}
	bg, err := parseHexColour(backgroundColour)
	if err != nil {
		return "", err
	}
	pr, err := parseHexColour(primaryColour)
	if err != nil {
		return "", err
	}
	style := normalizeBackgroundStyle(styleID)
	if !IsBackgroundStyle(style) {
		return "", fmt.Errorf("media: 未知的背景样式 %q；可用：%s",
			styleID, strings.Join(sortedBackgroundStyles(), ", "))
	}

	base := fmt.Sprintf("color=c=%s:s=%dx%d:r=%d", bg, w, h, fps)
	switch style {
	case "solid":
		return base, nil
	case "auto", "gradient":
		// auto 在预览里用渐变代表"由模型决定"——总得画点什么出来。
		mid, berr := blendHexColour(bg, pr, 0.22)
		if berr != nil {
			return "", berr
		}
		// `seed` 必须固定：gradients 默认带随机种子，同一套设置每次渲染出的
		// 渐变都不一样。预览要能"调一次、看一眼、再调一次"地反复比对，
		// 每次长得不同会让人以为是自己改坏了。（实测就是它导致 auto 与
		// gradient 的帧指纹不同。）
		return fmt.Sprintf("gradients=s=%dx%d:r=%d:c0=%s:c1=%s:x0=0:y0=0:x1=%d:y1=%d:seed=42",
			w, h, fps, bg, mid, w, h), nil
	case "grid":
		cell := maxInt(12, w/30)
		return base + fmt.Sprintf(",drawgrid=w=%d:h=%d:t=1:c=%s@0.12", cell, cell, pr), nil
	case "vignette":
		return base + ",vignette=PI/4", nil
	case "noise":
		return base + ",noise=alls=12:allf=t+u", nil
	case "scanlines":
		lineH := maxInt(2, h/270)
		// `x=w` 把 drawgrid 的**竖向**线条推出画面：这个滤镜同时画竖线与横线，
		// 而扫描线只要横线。实测 x=0 时第 0 列比第 100 列亮 7（肉眼能看出一条竖边），
		// x=w 后两列数值完全一致。
		return base + fmt.Sprintf(",drawgrid=w=%d:h=%d:t=1:c=%s@0.10:x=%d:y=0",
			w*10, lineH, pr, w), nil
	}
	return "", fmt.Errorf("media: 未处理的背景样式 %q", style)
}

// sortedBackgroundStyles 返回排序后的 id 列表，用于错误信息。
func sortedBackgroundStyles() []string {
	out := BackgroundStyleIDs()
	sort.Strings(out)
	return out
}

// TestCardFilter 在背景之上叠一张**测试卡**。
//
// 为什么用色块而不是文字：Go 侧没有任何 drawtext/字体机制（那套在 Python 的
// stock 渲染器里），而本项目已经在 drawtext 的 fontfile 转义上栽过一次。
// 测试卡要回答的问题其实不需要文字：
//
//	灰阶梯 -> 对比度、亮度、以及"压暗部"是否真的压了
//	三色块 -> 色温与白平衡偏没偏
//	主色条 -> 调色板在画面上的实际观感
//
// **文字交给真实的字幕烧录路径**（PostProcess）：字幕预览因此走的不是近似，
// 而是与成片完全相同的那段代码。
func TestCardFilter(backgroundColour, primaryColour string, w, h int) string {
	gray := func(level int) string {
		return fmt.Sprintf("0x%02X%02X%02X", level, level, level)
	}
	var boxes []string
	add := func(x, y, bw, bh int, colour string) {
		boxes = append(boxes, fmt.Sprintf(
			"drawbox=x=%d:y=%d:w=%d:h=%d:color=%s:t=fill", x, y, bw, bh, colour))
	}

	barH := maxInt(6, h/10)
	// 灰阶梯：8 级从黑到白，放在左下角。
	swatch := maxInt(8, w/16)
	for i := 0; i < 8; i++ {
		add(w/16+i*swatch, h*62/100, swatch, barH, gray(i*32))
	}
	// 三色块：红/绿/蓝并排，用来判断色温与白平衡。
	add(w*62/100, h*62/100, swatch, barH, "red")
	add(w*62/100+swatch, h*62/100, swatch, barH, "green")
	add(w*62/100+swatch*2, h*62/100, swatch, barH, "blue")
	// 主色条：一条粗的（像主标题）+ 一条细的（像副标题/正文），
	// 用来看调色板落在画面上的实际观感。
	add(w/10, h*30/100, w*50/100, maxInt(8, h/22), primaryColour)
	add(w/10, h*42/100, w*35/100, maxInt(4, h/48), gray(160))

	return strings.Join(boxes, ",")
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

// ---------------------------------------------------------------------------
// 风格预设配色
// ---------------------------------------------------------------------------

// StylePreset 是风格预设的配色。
//
// **生成期的真源在 Python**（`schemas.STYLE_PRESETS`）：它负责把预设展开进提示词。
// 这里这份只服务于效果预览 —— 预览要自己算出背景与主色，否则就得为了画一张
// 3 秒的预览回一次 AI 服务，代价与收益完全不成比例。
//
// 两边由 `ai/tests/test_backgrounds.py` 逐项钉住（与背景样式 id 同样的做法）。
// 一旦分叉，表现是"预览的颜色和成片不一样" —— 用户会照着预览去调参数，
// 所以这种不一致必须被测试拦住，而不能靠人记得同步。
type StylePreset struct {
	ID              string
	PrimaryColor    string
	BackgroundColor string
}

// stylePresets 与 Python 的 STYLE_PRESETS 对称。
var stylePresets = []StylePreset{
	{ID: "default", PrimaryColor: "#4F8CFF", BackgroundColor: "#0B1020"},
	{ID: "tech", PrimaryColor: "#3DDC97", BackgroundColor: "#08111F"},
	{ID: "warm", PrimaryColor: "#FF8A4C", BackgroundColor: "#1A1013"},
	{ID: "minimal", PrimaryColor: "#E6ECFF", BackgroundColor: "#101216"},
	{ID: "nature", PrimaryColor: "#5FD68A", BackgroundColor: "#0B1A12"},
	{ID: "sunset", PrimaryColor: "#FF6B9D", BackgroundColor: "#1B1020"},
}

// StylePresets 返回全部风格预设（副本）。
func StylePresets() []StylePreset {
	out := make([]StylePreset, len(stylePresets))
	copy(out, stylePresets)
	return out
}

// LookupStylePreset 按 id 取预设；未登记时返回 false（由调用方决定是报错还是用缺省）。
func LookupStylePreset(id string) (StylePreset, bool) {
	n := strings.ToLower(strings.TrimSpace(id))
	if n == "" {
		n = "default"
	}
	for _, p := range stylePresets {
		if p.ID == n {
			return p, true
		}
	}
	return StylePreset{}, false
}
