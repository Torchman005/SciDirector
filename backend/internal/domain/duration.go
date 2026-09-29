package domain

import (
	"math"
	"strings"
	"unicode"
)

// 时长估算的取值依据。
//
// 语速不是拍脑袋定的：中文旁白通常在 200~260 字/分钟，也就是 **3.3~4.3 字/秒**。
// 取 4.2 是偏快的一端 —— 科普片配画面后语速普遍偏快，而且估算偏短比偏长好：
// 偏短时导演会压缩文案，偏长则会塞进更多"垫话"，后者更容易变成水词。
const (
	// cjkPerSecond 是中文（含日韩汉字/假名）的说话速度。
	cjkPerSecond = 4.2
	// wordsPerSecond 是拉丁词的速度（英文旁白约 150~160 词/分钟）。
	wordsPerSecond = 2.6
	// minEstimateSec / maxEstimateSec 是估算的夹取区间。
	//
	// 下限 15 秒：再短就不像一部成片，而像一个片段；
	// 上限 10 分钟：与 HTTP 层 target_duration_sec 的上限（1800）留足余量，
	// 但又避免把一整本书的脚本估成一个不现实的天文数字。
	minEstimateSec = 15.0
	maxEstimateSec = 600.0
)

// EstimateDurationSec 按脚本内容估算一个合理的成片时长。
//
// 为什么要有它：目标时长原先是一个写死的缺省值（90 秒），与脚本长短无关 ——
// 一段 60 字的短文案会被撑成 90 秒（塞满水词），一段 800 字的长文又会被压到 90 秒
// （每句都赶）。**用户没填时长时，脚本本身就是最好的依据**。
//
// 计数方式是"说话单位"而不是字符数：一个汉字算一个单位，一串拉丁字母算一个词。
// 直接数字符会把英文脚本高估三倍以上（一个 5 字母的单词只念一次）。
func EstimateDurationSec(script string) float64 {
	cjk, words := countSpeechUnits(script)
	if cjk == 0 && words == 0 {
		// 空脚本（或全是标点/空白）走下限：调用方通常还会做非空校验，
		// 这里给一个可用的值而不是 0，免得下游把 0 当成"没设置"再绕一圈。
		return minEstimateSec
	}

	seconds := float64(cjk)/cjkPerSecond + float64(words)/wordsPerSecond
	seconds = math.Max(seconds, minEstimateSec)
	seconds = math.Min(seconds, maxEstimateSec)
	// 取整到 5 秒：估算本来就有 ±20% 的误差，给出 47.3 秒这种数字是虚假的精确。
	return math.Round(seconds/5) * 5
}

// countSpeechUnits 数出"要说出口"的单位：汉字数与拉丁词数。
//
// 标点、空白、Markdown 记号都不计数 —— 它们不发音。
// 中文标点（，。！？）虽然对应停顿，但停顿已经体现在语速常数里，
// 再算一遍会双重计数。
func countSpeechUnits(script string) (cjk int, words int) {
	inWord := false
	for _, r := range script {
		switch {
		case isCJKIdeograph(r) || unicode.Is(unicode.Hiragana, r) || unicode.Is(unicode.Katakana, r):
			cjk++
			inWord = false
		case unicode.IsLetter(r) || unicode.IsDigit(r):
			// 拉丁字母/数字：一串连续的算一个词。
			if !inWord {
				words++
				inWord = true
			}
		default:
			// 标点、空白、Markdown 记号：终止当前单词。
			inWord = false
		}
	}
	return cjk, words
}

// isCJKIdeograph 判断是否是需要逐字计数的表意文字。
//
// 用区间判断而不是 `unicode.Is(unicode.Han, r)`：后者会把扩展区的生僻字也算进来，
// 而那对语速统计没有意义；这里只需要覆盖常用汉字所在的几个基本区。
func isCJKIdeograph(r rune) bool {
	return (r >= 0x4E00 && r <= 0x9FFF) || // CJK 统一表意文字
		(r >= 0x3400 && r <= 0x4DBF) || // 扩展 A
		(r >= 0xF900 && r <= 0xFAFF) // 兼容表意文字
}

// StripScriptNoise 去掉脚本里的常见噪声，便于估算与统计。
//
// 目前只做保守清理：去掉首尾空白。刻意**不**剥离 Markdown 记号与代码块 ——
// 科普脚本里的代码块往往是要念出来的，删掉会低估时长；
// 而记号和标点本来就不计入说话单位（见 countSpeechUnits）。
func StripScriptNoise(script string) string {
	return strings.TrimSpace(script)
}
