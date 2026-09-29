package domain

import (
	"strings"
	"testing"
)

func TestEstimateDurationClampsEmptyScript(t *testing.T) {
	// 空脚本、纯标点、纯空白都走下限 —— 给一个可用值而不是 0，
	// 免得下游把 0 当成"没设置"再绕一圈。
	for _, s := range []string{"", "   ", "。，！？", "\n\n"} {
		if got := EstimateDurationSec(s); got != minEstimateSec {
			t.Errorf("EstimateDurationSec(%q) = %v，期望下限 %v", s, got, minEstimateSec)
		}
	}
}

func TestEstimateDurationCountsChineseByCharacter(t *testing.T) {
	// 210 个汉字 / 4.2 字每秒 = 50 秒。
	got := EstimateDurationSec(strings.Repeat("字", 210))
	if got != 50 {
		t.Errorf("210 个汉字应当估成 50 秒，实际 %v", got)
	}
}

func TestEstimateDurationCountsEnglishByWordNotByCharacter(t *testing.T) {
	// 这条是**反向对照**：260 个英文单词（共 1300 个字符）应当估成 100 秒。
	// 如果实现是"数字符"，会得到 1300/4.2 ≈ 310 秒 —— 高估三倍以上。
	script := strings.Repeat("word ", 260)
	got := EstimateDurationSec(script)
	if got != 100 {
		t.Errorf("260 个英文单词应当估成 100 秒，实际 %v（按字符数会得到约 310，说明数错了单位）", got)
	}
}

func TestEstimateDurationCombinesBothLanguages(t *testing.T) {
	// 42 个汉字（10s）+ 26 个词（10s）= 20s。
	script := strings.Repeat("字", 42) + " " + strings.Repeat("word ", 26)
	got := EstimateDurationSec(script)
	if got != 20 {
		t.Errorf("中英混排应当估成 20 秒，实际 %v", got)
	}
}

func TestEstimateDurationIsMonotonic(t *testing.T) {
	// 脚本越长，估算时长不能反而变小 —— 这类"看着差不多"的破坏
	// （比如取整写成了截断）只有靠性质断言才拦得住。
	prev := 0.0
	for _, n := range []int{10, 50, 100, 300, 900, 2400} {
		got := EstimateDurationSec(strings.Repeat("这是一个科普脚本的内容。", n/12+1))
		if got < prev {
			t.Fatalf("脚本变长后估算反而变小：%v -> %v", prev, got)
		}
		prev = got
	}
}

func TestEstimateDurationClampsUpperBound(t *testing.T) {
	// 大约 4200 个汉字原始估算是 1000 秒，必须被夹到上限。
	got := EstimateDurationSec(strings.Repeat("字", 4200))
	if got != maxEstimateSec {
		t.Errorf("超长脚本应当夹到上限 %v，实际 %v", maxEstimateSec, got)
	}
}

func TestEstimateDurationRoundsToFiveSeconds(t *testing.T) {
	// 估算本身有 ±20% 误差，给出 47.3 秒是虚假的精确。
	for n := 100; n < 400; n += 7 {
		got := EstimateDurationSec(strings.Repeat("字", n))
		if int(got)%5 != 0 {
			t.Fatalf("%d 个汉字的估算 %v 不是 5 的整数倍", n, got)
		}
	}
}

func TestEstimateDurationIgnoresPunctuationAndMarkdown(t *testing.T) {
	// 标点与 Markdown 记号不发音，不该被算进时长。
	plain := strings.Repeat("字", 210)
	decorated := "## " + strings.Repeat("字，", 210) + "\n\n**加粗**"
	plainSec := EstimateDurationSec(plain)
	decoratedSec := EstimateDurationSec(decorated)

	// 装饰版多出的只有"加粗"两字（2 个汉字），差别应当很小；
	// 若把标点也算进去，装饰版会多出 210 个单位的时长。
	if decoratedSec >= plainSec+15 {
		t.Errorf("标点被算进了时长：纯文本 %v 秒，带装饰 %v 秒", plainSec, decoratedSec)
	}
}
