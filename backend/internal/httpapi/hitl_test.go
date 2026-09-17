package httpapi

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/itJinYu/SciDirector/backend/internal/ai"
	"github.com/itJinYu/SciDirector/backend/internal/config"
	"github.com/itJinYu/SciDirector/backend/internal/domain"
	"github.com/itJinYu/SciDirector/backend/internal/queue"
	"github.com/itJinYu/SciDirector/backend/internal/store"
	"github.com/itJinYu/SciDirector/backend/internal/ws"
)

// 这些测试覆盖阶段四的人工闭环在 **HTTP 层**的语义。
// 它们需要真实 Redis（Store 与队列都建在上面），不可达时跳过 ——
// 跳过而不是失败，是为了让没有 Redis 的 CI 不变红，
// 同时保证本地（Redis 在跑）时真的执行到。

func testRedisAddr(t *testing.T) string {
	t.Helper()
	addr := "127.0.0.1:6379"
	conn, err := net.DialTimeout("tcp", addr, 500*time.Millisecond)
	if err != nil {
		t.Skipf("Redis 不可达（%s），跳过 HTTP 集成测试: %v", addr, err)
	}
	_ = conn.Close()
	return addr
}

type harness struct {
	router *gin.Engine
	store  *store.Store
	queue  *queue.Client
	ctx    context.Context
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	gin.SetMode(gin.TestMode)
	addr := testRedisAddr(t)

	redisCfg := config.RedisConfig{Addr: addr, DB: 14}
	ctx := context.Background()

	st, err := store.New(ctx, redisCfg)
	if err != nil {
		t.Fatalf("构造 Store 失败: %v", err)
	}
	t.Cleanup(func() { _ = st.Close() })

	cfg := &config.Config{
		Env:      "test",
		LogLevel: "error",
		HTTP:     config.HTTPConfig{CORSAllowedOrigins: []string{"*"}},
		Redis:    redisCfg,
		Queue:    config.QueueConfig{Queues: map[string]int{"critical": 6, "default": 3}},
	}
	q := queue.NewClient(redisCfg, cfg.Queue)
	t.Cleanup(func() { _ = q.Close() })

	aiClient, err := ai.NewClient(config.AIConfig{
		Addr: "127.0.0.1:59999", // 不会被调用；构造是惰性的
	}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatalf("构造 ai 客户端失败: %v", err)
	}
	t.Cleanup(func() { _ = aiClient.Close() })

	deps := Deps{
		Config: cfg,
		Store:  st,
		Queue:  q,
		AI:     aiClient,
		Hub:    ws.NewHub(nil, slog.New(slog.NewTextHandler(io.Discard, nil))),
	}
	return &harness{
		router: NewRouter(NewServer(deps), deps),
		store:  st,
		queue:  q,
		ctx:    ctx,
	}
}

// seedJob 写入一个带若干分镜的任务。
func (h *harness) seedJob(t *testing.T, jobID string, statuses ...domain.ShotStatus) *domain.Job {
	t.Helper()
	job := &domain.Job{
		JobID:             jobID,
		RawScript:         "测试脚本内容需要足够长以通过校验，这里补充一些文字凑够长度。",
		TargetDurationSec: 60,
		Locale:            "zh-CN",
		Status:            domain.JobRendering,
		CreatedAt:         time.Now().UTC(),
	}
	for i, st := range statuses {
		job.Shots = append(job.Shots, &domain.Shot{
			ShotID:      domain.ShotID(jobID, i),
			Index:       i,
			Tag:         domain.TagAmbience,
			Engine:      "stock",
			DurationSec: 4,
			Narration:   "原始画外音",
			VisualBrief: "原始画面说明",
			Status:      st,
			Attempt:     1,
		})
	}
	if err := h.store.SaveJob(h.ctx, job); err != nil {
		t.Fatalf("写入测试任务失败: %v", err)
	}
	return job
}

// decodeOK 拆开统一的成功信封 `{"ok":true,"data":…}` 并反序列化其中的负载。
//
// 直接 Unmarshal 到业务结构体会得到全零值 —— 因为响应的顶层是信封，
// 不是负载本身。这个坑在本项目里真实发生过（前端曾因此所有字段都是 undefined），
// 因此这里显式封装，避免每个用例各写一遍。
func decodeOK[T any](t *testing.T, w *httptest.ResponseRecorder) T {
	t.Helper()
	var env struct {
		OK   bool            `json:"ok"`
		Data json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil {
		t.Fatalf("解析响应信封失败: %v；响应体=%s", err, w.Body.String())
	}
	if !env.OK {
		t.Fatalf("响应 ok=false：%s", w.Body.String())
	}
	var out T
	if len(env.Data) > 0 {
		if err := json.Unmarshal(env.Data, &out); err != nil {
			t.Fatalf("解析响应负载失败: %v；负载=%s", err, string(env.Data))
		}
	}
	return out
}

func (h *harness) do(t *testing.T, method, path string, body any) *httptest.ResponseRecorder {
	t.Helper()
	var reader *bytes.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatalf("序列化请求体失败: %v", err)
		}
		reader = bytes.NewReader(raw)
	} else {
		reader = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, path, reader)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	w := httptest.NewRecorder()
	h.router.ServeHTTP(w, req)
	return w
}

// ---------------------------------------------------------------------------
// C4：打回一个镜头只影响它自己
// ---------------------------------------------------------------------------

func TestRejectShotOnlyAffectsTargetShot(t *testing.T) {
	h := newHarness(t)
	jobID := "test-reject-" + time.Now().Format("150405.000000")
	job := h.seedJob(t, jobID, domain.StatusApproved, domain.StatusApproved, domain.StatusApproved)

	target := job.Shots[1].ShotID
	w := h.do(t, http.MethodPost, "/api/v1/jobs/"+jobID+"/shots/"+target+"/reject",
		map[string]string{"comment": "坐标轴标签重叠，请旋转 45 度并缩小字号"})

	if w.Code != http.StatusAccepted && w.Code != http.StatusOK {
		t.Fatalf("打回应被受理，实际 HTTP %d：%s", w.Code, w.Body.String())
	}

	got, err := h.store.GetJob(h.ctx, jobID)
	if err != nil {
		t.Fatalf("读取任务失败: %v", err)
	}

	// 核心断言：只有被点名的镜头状态变了。
	for _, s := range got.Shots {
		if s.ShotID == target {
			if s.Status != domain.StatusRetrying {
				t.Errorf("目标镜头状态 = %s，期望 RETRYING", s.Status)
			}
			if s.Attempt != 2 {
				t.Errorf("目标镜头 attempt = %d，期望 2（人工打回同样消耗一次额度）", s.Attempt)
			}
			if len(s.Feedbacks) != 1 {
				t.Errorf("目标镜头应记录 1 条意见，实际 %d", len(s.Feedbacks))
			}
		} else {
			if s.Status != domain.StatusApproved {
				t.Errorf("非目标镜头 %s 的状态被改动了：%s", s.ShotID, s.Status)
			}
			if s.Attempt != 1 {
				t.Errorf("非目标镜头 %s 的 attempt 被改动了：%d", s.ShotID, s.Attempt)
			}
			if len(s.Feedbacks) != 0 {
				t.Errorf("非目标镜头 %s 被写入了意见", s.ShotID)
			}
		}
	}

	// 任务整体回到渲染中。
	if got.Status != domain.JobRendering {
		t.Errorf("任务状态 = %s，期望 RENDERING", got.Status)
	}
}

// ---------------------------------------------------------------------------
// C5：熔断镜头放行后任务继续合成
// ---------------------------------------------------------------------------

func TestApproveLastShotEnqueuesCompose(t *testing.T) {
	h := newHarness(t)
	jobID := "test-approve-" + time.Now().Format("150405.000000")
	// 两个已通过、一个熔断待人工。
	job := h.seedJob(t, jobID, domain.StatusApproved, domain.StatusApproved, domain.StatusAwaitingHuman)

	target := job.Shots[2].ShotID
	w := h.do(t, http.MethodPost, "/api/v1/jobs/"+jobID+"/shots/"+target+"/approve", nil)
	if w.Code != http.StatusOK {
		t.Fatalf("放行应成功，实际 HTTP %d：%s", w.Code, w.Body.String())
	}

	resp := decodeOK[ApproveResponse](t, w)
	// 这是本次修复的核心：放行**必须**顺带把合成任务入队。
	// 只改状态的话，任务会永远停在 COMPOSING，前端看着 100% 却等不到成片。
	if !resp.ComposeEnqueued {
		t.Fatalf("最后一个镜头放行后应报告已入队合成任务；响应体=%s", w.Body.String())
	}

	got, err := h.store.GetJob(h.ctx, jobID)
	if err != nil {
		t.Fatalf("读取任务失败: %v", err)
	}
	if got.Status != domain.JobComposing {
		t.Fatalf("任务状态 = %s，期望 COMPOSING", got.Status)
	}
	if st := got.Stat(); st.Approved != 3 {
		t.Fatalf("已通过数 = %d，期望 3", st.Approved)
	}
}

func TestApproveDoesNotEnqueueComposeWhenOthersPending(t *testing.T) {
	h := newHarness(t)
	jobID := "test-approve-partial-" + time.Now().Format("150405.000000")
	job := h.seedJob(t, jobID, domain.StatusAwaitingHuman, domain.StatusAwaitingHuman)

	// 放行第二个；第一个仍待人工。
	target := job.Shots[1].ShotID
	w := h.do(t, http.MethodPost, "/api/v1/jobs/"+jobID+"/shots/"+target+"/approve", nil)
	if w.Code != http.StatusOK {
		t.Fatalf("放行应成功，实际 HTTP %d：%s", w.Code, w.Body.String())
	}

	resp := decodeOK[ApproveResponse](t, w)
	// 还有镜头没通过时不应触发合成 —— 那会产出一部中间缺镜头的成片。
	if resp.ComposeEnqueued {
		t.Fatal("仍有镜头未通过时不应入队合成")
	}
	after, _ := h.store.GetJob(h.ctx, jobID)
	if after.Status == domain.JobComposing {
		t.Fatal("仍有镜头未通过时任务不应进入 COMPOSING")
	}
}

// ---------------------------------------------------------------------------
// 成分镜编辑
// ---------------------------------------------------------------------------

func TestPatchShotUpdatesOnlyGivenFields(t *testing.T) {
	h := newHarness(t)
	jobID := "test-patch-" + time.Now().Format("150405.000000")
	job := h.seedJob(t, jobID, domain.StatusApproved)
	target := job.Shots[0].ShotID

	w := h.do(t, http.MethodPatch, "/api/v1/jobs/"+jobID+"/shots/"+target,
		map[string]any{"narration": "改过的画外音", "redo": false})
	if w.Code != http.StatusOK {
		t.Fatalf("编辑应成功，实际 HTTP %d：%s", w.Code, w.Body.String())
	}

	resp := decodeOK[PatchShotResponse](t, w)
	if len(resp.Changed) != 1 || resp.Changed[0] != "narration" {
		t.Fatalf("changed = %v，期望只有 narration；响应体=%s", resp.Changed, w.Body.String())
	}

	got, _ := h.store.GetJob(h.ctx, jobID)
	if got.Shots[0].Narration != "改过的画外音" {
		t.Errorf("narration 未更新：%q", got.Shots[0].Narration)
	}
	// 未给出的字段必须原样保留 —— 这是 PATCH 与 PUT 的分界线。
	if got.Shots[0].VisualBrief != "原始画面说明" {
		t.Errorf("未给出的 visual_brief 被改动了：%q", got.Shots[0].VisualBrief)
	}
	// redo=false 时不应改变状态或消耗额度。
	if got.Shots[0].Status != domain.StatusApproved || got.Shots[0].Attempt != 1 {
		t.Errorf("redu=false 时不应改动状态/额度，实际 %s/%d",
			got.Shots[0].Status, got.Shots[0].Attempt)
	}
}

func TestPatchShotCanClearField(t *testing.T) {
	h := newHarness(t)
	jobID := "test-patch-clear-" + time.Now().Format("150405.000000")
	job := h.seedJob(t, jobID, domain.StatusApproved)
	target := job.Shots[0].ShotID

	// 显式传空串 == 清空。这是「指针字段」与「零值判断」的分界线：
	// 用值类型的话，空串会被当成「未给出」而静默不生效。
	w := h.do(t, http.MethodPatch, "/api/v1/jobs/"+jobID+"/shots/"+target,
		map[string]any{"narration": "", "redo": false})
	if w.Code != http.StatusOK {
		t.Fatalf("清空字段应成功，实际 HTTP %d：%s", w.Code, w.Body.String())
	}

	got, _ := h.store.GetJob(h.ctx, jobID)
	if got.Shots[0].Narration != "" {
		t.Fatalf("narration 应被清空，实际 %q", got.Shots[0].Narration)
	}
}

func TestPatchShotRejectsEmptyRequest(t *testing.T) {
	h := newHarness(t)
	jobID := "test-patch-empty-" + time.Now().Format("150405.000000")
	job := h.seedJob(t, jobID, domain.StatusApproved)
	target := job.Shots[0].ShotID

	// 什么都没给：请求无意义，应当明确拒绝而不是返回 200 让人以为生效了。
	w := h.do(t, http.MethodPatch, "/api/v1/jobs/"+jobID+"/shots/"+target,
		map[string]any{"redo": false})
	if w.Code != http.StatusBadRequest {
		t.Fatalf("空请求应返回 400，实际 HTTP %d：%s", w.Code, w.Body.String())
	}
}

func TestPatchShotWithRedoConsumesAttempt(t *testing.T) {
	h := newHarness(t)
	jobID := "test-patch-redo-" + time.Now().Format("150405.000000")
	job := h.seedJob(t, jobID, domain.StatusApproved)
	target := job.Shots[0].ShotID

	w := h.do(t, http.MethodPatch, "/api/v1/jobs/"+jobID+"/shots/"+target,
		map[string]any{"visual_brief": "换成更好的画面", "redo": true, "comment": "画面太挤"})
	if w.Code != http.StatusOK {
		t.Fatalf("编辑并重做应成功，实际 HTTP %d：%s", w.Code, w.Body.String())
	}

	got, _ := h.store.GetJob(h.ctx, jobID)
	s := got.Shots[0]
	if s.Status != domain.StatusRetrying {
		t.Errorf("redo=true 后状态 = %s，期望 RETRYING", s.Status)
	}
	if s.Attempt != 2 {
		t.Errorf("redo=true 后 attempt = %d，期望 2", s.Attempt)
	}
	if s.VisualBrief != "换成更好的画面" {
		t.Errorf("visual_brief 未更新：%q", s.VisualBrief)
	}
	if len(s.Feedbacks) != 1 {
		t.Errorf("应记录 1 条意见，实际 %d", len(s.Feedbacks))
	}
}

func TestPatchShotNotFound(t *testing.T) {
	h := newHarness(t)
	jobID := "test-patch-404-" + time.Now().Format("150405.000000")
	h.seedJob(t, jobID, domain.StatusApproved)

	w := h.do(t, http.MethodPatch, "/api/v1/jobs/"+jobID+"/shots/does-not-exist",
		map[string]any{"narration": "x"})
	if w.Code != http.StatusNotFound {
		t.Fatalf("分镜不存在应返回 404，实际 HTTP %d", w.Code)
	}
}
