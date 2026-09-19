package domain

// 本文件覆盖**阶段五成本核算**的推导部分。
//
// 为什么推导部分值得单独测：
// 它没有 IO、没有依赖，唯一的风险就是**算错** —— 而算错的成本数字不会报错，
// 只会安静地把一笔开销记成三倍或记成零。因此这里的重点是**反向控制**：
// 不仅断言「该算的算上了」，也断言「不该算的没被算上」。

import (
	"encoding/json"
	"testing"
)

func TestCostSnapshotSumsRenderSeconds(t *testing.T) {
	job := &Job{
		JobID: "job-1",
		Shots: []*Shot{
			{ShotID: "s1", Status: StatusApproved, Artifact: &Artifact{RenderCostSec: 2.5}},
			{ShotID: "s2", Status: StatusApproved, Artifact: &Artifact{RenderCostSec: 1.25}},
			// 未渲染完的镜头：还没有产物，不该贡献渲染时长。
			{ShotID: "s3", Status: StatusRendering},
			// 有产物但没记录耗时：按 0 处理，不能因为缺字段就让整个成本变成 NaN。
			{ShotID: "s4", Status: StatusApproved, Artifact: &Artifact{RenderCostSec: 0}},
		},
	}

	got := job.CostSnapshot()
	if got.RenderSec != 3.75 {
		t.Fatalf("render_sec 应为 3.75，实际 %v", got.RenderSec)
	}
	if got.Shots != 4 {
		t.Fatalf("shots 应为 4，实际 %d", got.Shots)
	}
	if got.Approved != 3 {
		t.Fatalf("approved 应为 3，实际 %d", got.Approved)
	}
}

// 中文一个字是 3 个字节。用 len() 会把 TTS 成本算成三倍 ——
// 这是个「看起来能用」的错误：数字大小合理，只有对着服务商账单才会发现。
func TestCostSnapshotCountsTTSCharsByRuneNotByte(t *testing.T) {
	narration := "神经网络的反向传播"
	job := &Job{
		JobID: "job-1",
		Shots: []*Shot{
			{ShotID: "s1", Narration: narration, Artifact: &Artifact{AudioPath: "/a.mp3"}},
		},
	}

	got := job.CostSnapshot()
	if want := len([]rune(narration)); got.TTSChars != want {
		t.Fatalf("tts_chars 应为 %d（按字符），实际 %d（按字节会是 %d）",
			want, got.TTSChars, len(narration))
	}
	if got.TTSShots != 1 {
		t.Fatalf("tts_shots 应为 1，实际 %d", got.TTSShots)
	}
}

// 反向控制：接了 TTS 但某个镜头没配上音（服务商失败），
// 那个镜头的字数**不能**计入成本 —— 否则 TTS 整体挂掉时成本看起来照样正常，
// 正好掩盖了真正的问题。
func TestCostSnapshotExcludesShotsWithoutAudio(t *testing.T) {
	job := &Job{
		JobID: "job-1",
		Shots: []*Shot{
			{ShotID: "s1", Narration: "有配音的镜头", Artifact: &Artifact{AudioPath: "/a.mp3"}},
			{ShotID: "s2", Narration: "没配上音的镜头", Artifact: &Artifact{AudioPath: ""}},
			{ShotID: "s3", Narration: "还没渲染的镜头"},
		},
	}

	got := job.CostSnapshot()
	if want := len([]rune("有配音的镜头")); got.TTSChars != want {
		t.Fatalf("只应统计有配音的镜头：期望 %d，实际 %d", want, got.TTSChars)
	}
	if got.TTSShots != 1 {
		t.Fatalf("tts_shots 应为 1，实际 %d", got.TTSShots)
	}
}

func TestCostSnapshotCarriesPersistedLLMUsage(t *testing.T) {
	job := &Job{
		JobID:    "job-1",
		LLMUsage: &LLMUsage{PromptTokens: 100, CompletionTokens: 250, TotalTokens: 350, Calls: 3},
	}

	got := job.CostSnapshot()
	if got.LLM.TotalTokens != 350 || got.LLM.Calls != 3 {
		t.Fatalf("LLM 用量未带出: %+v", got.LLM)
	}
	if got.LLM.PromptTokens+got.LLM.CompletionTokens != got.LLM.TotalTokens {
		t.Fatalf("上报数据本身不自洽: %+v", got.LLM)
	}
}

// 边界：nil 任务、nil 镜头、nil 产物都出现在真实数据里
// （分镜表解析失败会留下 nil 槽位；未渲染的镜头没有产物）。
// 这些路径一旦 panic，读成本接口就会 500，而成本接口恰恰是排查问题时要用的。
func TestCostSnapshotToleratesNilInputs(t *testing.T) {
	var nilJob *Job
	if got := nilJob.CostSnapshot(); got != (Cost{}) {
		t.Fatalf("nil 任务应返回零值成本，实际 %+v", got)
	}

	job := &Job{JobID: "job-1", Shots: []*Shot{nil, {ShotID: "s1"}, nil}}
	got := job.CostSnapshot()
	if got.Shots != 3 {
		t.Fatalf("shots 计入 nil 槽位以反映真实数组长度，实际 %d", got.Shots)
	}
	if got.RenderSec != 0 || got.TTSChars != 0 {
		t.Fatalf("nil 槽位不该贡献任何用量: %+v", got)
	}
}

// 反向控制：没有产物时，推导项必须是 0 ——
// 不能因为持久化里有 LLM 用量就顺手把别的字段也「顺」出一个非零值。
func TestCostSnapshotNeverReportsHalfFilledUsage(t *testing.T) {
	job := &Job{
		JobID:    "job-1",
		LLMUsage: &LLMUsage{TotalTokens: 350, Calls: 3},
	}
	got := job.CostSnapshot()
	if got.RenderSec != 0 || got.TTSChars != 0 || got.TTSShots != 0 {
		t.Fatalf("没有产物时推导项必须为 0: %+v", got)
	}
}

func TestApplyLLMUsageOverwritesNotAccumulates(t *testing.T) {
	job := &Job{JobID: "job-1"}

	job.ApplyLLMUsage(LLMUsage{PromptTokens: 1, CompletionTokens: 2, TotalTokens: 3, Calls: 1})
	// 第二次上报是**完整快照**而非增量。若实现成了累加，重跑一次任务
	// （断点续跑会重放 final 事件）成本就会翻倍。
	job.ApplyLLMUsage(LLMUsage{PromptTokens: 10, CompletionTokens: 20, TotalTokens: 30, Calls: 4})

	if job.LLMUsage.TotalTokens != 30 || job.LLMUsage.Calls != 4 {
		t.Fatalf("重复上报应整体覆盖，实际 %+v", job.LLMUsage)
	}
}

func TestApplyLLMUsageOnNilJobDoesNotPanic(t *testing.T) {
	var job *Job
	job.ApplyLLMUsage(LLMUsage{TotalTokens: 1}) // 不应 panic
}

func TestRoundToKeepsJSONStable(t *testing.T) {
	// 浮点求和会出现 0.30000000000000004 这类尾巴；
	// 不收敛的话同一份数据两次序列化可能不同，断言与人工比对都会变得不可靠。
	job := &Job{Shots: []*Shot{
		{Artifact: &Artifact{RenderCostSec: 0.1}},
		{Artifact: &Artifact{RenderCostSec: 0.2}},
	}}

	b1, err := json.Marshal(job.CostSnapshot())
	if err != nil {
		t.Fatalf("序列化失败: %v", err)
	}
	b2, _ := json.Marshal(job.CostSnapshot())
	if string(b1) != string(b2) {
		t.Fatalf("同一状态两次序列化不一致:\n%s\n%s", b1, b2)
	}
	if got := job.CostSnapshot().RenderSec; got != 0.3 {
		t.Fatalf("render_sec 应收敛为 0.3，实际 %v", got)
	}
}
