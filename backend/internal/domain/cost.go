package domain

// 任务成本核算。
//
// ## 两个类型，因为它们是两件事
//
//	LLMUsage —— Python 上报的 LLM 用量，随任务**持久化**（Go 推不出来：只有 Python 持有 LLM 客户端）；
//	Cost     —— 面向调用方的**成本快照**，读取时计算，含持久化的 LLM 部分 + 现算的推导部分。
//
// 刻意不合并成一个「部分字段有值」的类型：那种类型没有编译期约束，
// 很容易被某处顺手填上推导字段再写回存储，于是同一份数据出现两个来源。
// 用两个类型，编译器就替我们守住了「谁能写进存储」。
//
// ## 推导项不进持久化状态
//
// 渲染时长与配音字符数都能从任务状态推导（产物自带 render_cost_sec；有 audio_path
// 的镜头就是真的合成过配音的那几个）。把它们存起来就得同步 —— 而两边口径不一致
// 在成本这种数字上格外难查：没有报错、没有告警，只是数字悄悄偏了。
// 现算则天然与分镜表一致，不需要任何同步逻辑。
//
// ## 单位而不是金额
//
// 这里统计的是**资源用量**（token 数、秒数、字符数），不是钱。换算成金额需要单价表，
// 而单价随服务商、模型、时段变化；把单价写死在代码里等于制造一个
// 「看起来很精确但其实已过时」的数字。要金额就在上层按可配置的单价换算。

// LLMUsage 是 Python 上报的 LLM 用量。
type LLMUsage struct {
	PromptTokens     int `json:"prompt_tokens"`
	CompletionTokens int `json:"completion_tokens"`
	TotalTokens      int `json:"total_tokens"`
	Calls            int `json:"calls"`
}

// IsZero 报告是否没有任何用量。用于区分「还没跑到上报点」与「真的用了 0」，
// 让调用方不必靠 TotalTokens==0 去猜。
func (u LLMUsage) IsZero() bool {
	return u.PromptTokens == 0 && u.CompletionTokens == 0 && u.TotalTokens == 0 && u.Calls == 0
}

// Cost 是一个任务的成本快照（资源用量，非金额）。
type Cost struct {
	// LLM 是持久化的那一半（Python 上报）。
	LLM LLMUsage `json:"llm"`

	// RenderSec 是所有产物 render_cost_sec 之和，即真实渲染耗时。
	RenderSec float64 `json:"render_sec"`

	// TTSChars / TTSShots 只统计**真的产出了配音**的镜头。
	// 用「有 audio_path 的镜头」而不是「所有镜头」：没配上音的镜头不该计入 TTS 成本，
	// 否则接不上服务商时成本看起来照样正常，正好掩盖了真正的问题。
	TTSChars int `json:"tts_chars"`
	TTSShots int `json:"tts_shots"`

	// Shots / Approved 是让成本可解释的规模指标：
	// 「花了 4 万 token」只有配上「12 个镜头、返工 7 次」才说明得了问题。
	Shots    int `json:"shots"`
	Approved int `json:"approved"`
}

// CostSnapshot 返回当前任务状态下的成本快照。
//
// 这是一个**纯函数**（不碰 IO、不改状态），因此可以被完整单测；
// 也正因为推导部分现算，读到的一定与分镜表一致。
func (j *Job) CostSnapshot() Cost {
	var c Cost
	if j == nil {
		return c
	}
	if j.LLMUsage != nil {
		c.LLM = *j.LLMUsage
	}

	c.Shots = len(j.Shots)
	for _, s := range j.Shots {
		if s == nil {
			continue
		}
		if s.Status == StatusApproved {
			c.Approved++
		}
		if s.Artifact == nil {
			continue
		}
		if s.Artifact.RenderCostSec > 0 {
			c.RenderSec += s.Artifact.RenderCostSec
		}
		if s.Artifact.AudioPath != "" {
			// 有配音 → 这段画外音被合成过，字数是 TTS 的计价单位。
			// 用 rune 数而不是 len()：len 数的是字节，中文一个字算 3 个，
			// 而服务商按字符计费 —— 用 len 会把成本算成三倍。
			c.TTSShots++
			c.TTSChars += len([]rune(s.Narration))
		}
	}
	c.RenderSec = roundTo(c.RenderSec, 3)
	return c
}

// ApplyLLMUsage 把 Python 上报的 LLM 用量并入任务。
//
// 单独一个方法而不是让调用方直接赋值：推导项（渲染/配音）不属于存储，
// 谁都不该从事件里往任务的持久化字段上写 —— 那正是「两个来源」的开端。
func (j *Job) ApplyLLMUsage(u LLMUsage) {
	if j == nil {
		return
	}
	j.LLMUsage = &u
}

// roundTo 保留 n 位小数，让成本数字在 JSON 里稳定：
// 否则同一份数据每次序列化可能带出不同的小数尾巴，不利于比对与断言。
func roundTo(v float64, n int) float64 {
	pow := 1.0
	for i := 0; i < n; i++ {
		pow *= 10
	}
	return float64(int64(v*pow+0.5)) / pow
}
