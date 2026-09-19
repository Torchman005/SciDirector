package httpapi

// 本文件覆盖成本查询的 HTTP 语义（阶段五）。
//
// 关注点与纯计算的分开：domain 层已经证明了「怎么算」，
// 这里只证明「算出来的东西真的送到了调用方手上」——
// 以及最容易漏的那一点：**任务详情里也要有**。
// 只加端点而忘了详情响应，前端仍然拿不到成本，而端点测试会全绿。

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/itJinYu/SciDirector/backend/internal/domain"
)

// seedCostJob 写入一份带产物、带 LLM 用量的任务。
func (h *harness) seedCostJob(t *testing.T, jobID string) *domain.Job {
	t.Helper()
	job := h.seedJob(t, jobID, domain.StatusApproved, domain.StatusRendering)
	job.Shots[0].Narration = "第一段画外音"
	job.Shots[0].Artifact = &domain.Artifact{
		Engine:        "manim",
		RenderCostSec: 1.5,
		AudioPath:     "/work/" + jobID + "/shot_000/speech.mp3",
	}
	// 第二个镜头接了 TTS 但没配上音：它的字数不该出现在成本里。
	job.Shots[1].Narration = "没配上音的画外音"
	job.LLMUsage = &domain.LLMUsage{PromptTokens: 100, CompletionTokens: 250, TotalTokens: 350, Calls: 3}
	if err := h.store.SaveJob(h.ctx, job); err != nil {
		t.Fatalf("写入测试任务失败: %v", err)
	}
	return job
}

func TestHandleGetJobCost(t *testing.T) {
	h := newHarness(t)
	jobID := "job-cost-http-1"
	h.seedCostJob(t, jobID)

	w := httptest.NewRecorder()
	h.router.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/v1/jobs/"+jobID+"/cost", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("期望 200，实际 %d：%s", w.Code, w.Body.String())
	}

	got := decodeOK[CostResponse](t, w)
	if got.JobID != jobID {
		t.Fatalf("job_id 不符: %q", got.JobID)
	}
	if got.Cost.LLM.TotalTokens != 350 || got.Cost.LLM.Calls != 3 {
		t.Fatalf("LLM 用量未返回: %+v", got.Cost.LLM)
	}
	if got.Cost.RenderSec != 1.5 {
		t.Fatalf("render_sec 应为 1.5，实际 %v", got.Cost.RenderSec)
	}
	if want := len([]rune("第一段画外音")); got.Cost.TTSChars != want {
		t.Fatalf("tts_chars 应为 %d，实际 %d", want, got.Cost.TTSChars)
	}
	if got.Cost.TTSShots != 1 {
		t.Fatalf("tts_shots 应为 1，实际 %d", got.Cost.TTSShots)
	}
	if got.Cost.Shots != 2 || got.Cost.Approved != 1 {
		t.Fatalf("规模指标不符: %+v", got.Cost)
	}
}

// 详情里必须也带成本。前端拿任务详情渲染整个审核台，
// 若只有 /cost 端点，页面就得为一个数字多发一次请求 —— 而且很容易忘。
func TestHandleGetJobIncludesCost(t *testing.T) {
	h := newHarness(t)
	jobID := "job-cost-http-2"
	h.seedCostJob(t, jobID)

	w := httptest.NewRecorder()
	h.router.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/v1/jobs/"+jobID, nil))
	if w.Code != http.StatusOK {
		t.Fatalf("期望 200，实际 %d：%s", w.Code, w.Body.String())
	}

	got := decodeOK[JobResponse](t, w)
	if got.Cost.LLM.TotalTokens != 350 {
		t.Fatalf("详情响应缺少成本快照: %+v", got.Cost)
	}
	if got.Cost.RenderSec != 1.5 {
		t.Fatalf("详情里的成本未包含推导项: %+v", got.Cost)
	}
	// 持久化字段与快照是**两个概念**：job.llm_usage 是上报原文，
	// cost 是面向调用方的口径。两者都必须在，且不能互相冒充。
	if got.Job.LLMUsage == nil {
		t.Fatal("任务里应保留上报的 llm_usage")
	}
}

func TestHandleGetJobCostNotFound(t *testing.T) {
	h := newHarness(t)

	w := httptest.NewRecorder()
	h.router.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/v1/jobs/job-does-not-exist/cost", nil))
	if w.Code != http.StatusNotFound {
		t.Fatalf("不存在的任务应返回 404，实际 %d：%s", w.Code, w.Body.String())
	}
}

// 反向控制：没有任何产物的任务，成本必须是干净的 0，
// 而不是因为缺字段报错或返回 null（前端会渲染成 NaN）。
func TestHandleGetJobCostOnFreshJobIsZeroNotError(t *testing.T) {
	h := newHarness(t)
	jobID := "job-cost-http-3"
	h.seedJob(t, jobID, domain.StatusPending)

	w := httptest.NewRecorder()
	h.router.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/v1/jobs/"+jobID+"/cost", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("期望 200，实际 %d：%s", w.Code, w.Body.String())
	}

	got := decodeOK[CostResponse](t, w)
	if got.Cost.LLM.TotalTokens != 0 || got.Cost.RenderSec != 0 || got.Cost.TTSChars != 0 {
		t.Fatalf("未开工的任务成本应为 0: %+v", got.Cost)
	}
	if got.Cost.Shots != 1 {
		t.Fatalf("shots 仍应如实统计，实际 %d", got.Cost.Shots)
	}
}
