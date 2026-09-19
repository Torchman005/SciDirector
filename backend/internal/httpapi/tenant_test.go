package httpapi

// 本文件覆盖**多租户的归属与隔离**（阶段五）。
//
// ## 为什么核心用例是「按路由表遍历」
//
// 隔离最容易失败的方式不是「检查写错了」，而是「新加了一个按 job_id 的接口但忘了检查」。
// 逐个接口手写用例挡不住这件事 —— 漏掉的那个接口自然也没有用例。
// 因此这里从**路由表**里取出所有带 `:jobID` 的路径，逐个以「另一个租户」的身份去访问，
// 断言全部返回 404。新增接口时它会自动被覆盖，除非有人特意把它排除掉。
//
// ## 越权与不存在必须**不可区分**
//
// 断言的是 404 而不是 403：403 等于确认「这个 job_id 存在，只是不属于你」，
// 可被用来枚举有效任务 ID。因此这里连**响应体**一起比对 ——
// 只比对状态码的话，一个「404 + 不同的错误码/文案」的实现也能通过，
// 而那同样泄漏了存在性。
//
// ## 这是「归属与隔离」，不是身份认证
//
// 租户身份由请求头声明，能防住「拿别人的 job_id 去读」，防不住「伪造请求头」。
// 这一点写在 domain/tenant.go 里，也不该被本文件的绿色掩盖。

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sort"
	"strings"
	"testing"

	"github.com/itJinYu/SciDirector/backend/internal/domain"
)

const (
	tenantA = "tenant-a"
	tenantB = "tenant-b"
)

// seedTenantJob 写入一个属于指定租户的任务。
func (h *harness) seedTenantJob(t *testing.T, jobID, tenantID string) *domain.Job {
	t.Helper()
	job := h.seedJob(t, jobID, domain.StatusApproved, domain.StatusApproved)
	job.TenantID = tenantID
	if err := h.store.SaveJob(h.ctx, job); err != nil {
		t.Fatalf("写入测试任务失败: %v", err)
	}
	return job
}

// asTenant 发起一个带租户头的请求。
func (h *harness) asTenant(method, path, tenantID string, body string) *httptest.ResponseRecorder {
	var req *http.Request
	if body == "" {
		req = httptest.NewRequest(method, path, nil)
	} else {
		req = httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
	}
	req.Header.Set("X-Tenant-ID", tenantID)
	w := httptest.NewRecorder()
	h.router.ServeHTTP(w, req)
	return w
}

// jobScopedRoutes 从**真实路由表**里取出所有按 job_id 访问的路径。
//
// 这是本文件的关键：不手工维护列表，而是问路由器自己。
// 手写列表会在新增接口时悄悄过期，而"悄悄过期"正是隔离最容易失效的方式。
func (h *harness) jobScopedRoutes() []string {
	var out []string
	for _, r := range h.router.Routes() {
		if strings.Contains(r.Path, ":jobID") || strings.Contains(r.Path, ":jobId") {
			out = append(out, r.Path)
		}
	}
	sort.Strings(out)
	return out
}

// 用真实 jobID 替换路径参数，得到一个"路径形状正确、归属不对"的请求。
//
// 每个路由都要配一个**能通过参数校验**的请求体，这一点很关键：
// 校验发生在归属检查之前，用空体/坏体去试会在到达归属检查前就 400，
// 于是用例测的是参数校验而不是隔离 —— 我第一版就是这样，白白放过了
// approve/reject 返回 500 的真实缺陷，却误报了 PATCH 的"失败"。
func requestFor(route, jobID, shotID string) (method, path, body string) {
	switch {
	case strings.HasSuffix(route, "/reject"):
		return http.MethodPost, fill(route, jobID, shotID), `{"comment":"越权试探用的意见"}`
	case strings.HasSuffix(route, "/approve"):
		return http.MethodPost, fill(route, jobID, shotID), `{}`
	case strings.Contains(route, ":shotID"):
		return http.MethodPatch, fill(route, jobID, shotID), `{"narration":"越权试探"}`
	default:
		return http.MethodGet, fill(route, jobID, shotID), ""
	}
}

func fill(route, jobID, shotID string) string {
	path := strings.ReplaceAll(route, ":jobID", jobID)
	return strings.ReplaceAll(path, ":shotID", shotID)
}

// normalizeBody 去掉每次请求都不同、且与归属无关的字段，便于比较两种响应是否一致。
//
// `trace_id` 每个请求都不一样，直接比对整段 JSON 必然不等 ——
// 那是用例的错，不是实现的错（我第一版就写成了直接比对，四个路由全"失败"）。
// `detail` 一并去掉：它本身就是不该出现在越权响应里的东西，
// 由下面的 hasLeakyDetail 单独断言。
func normalizeBody(t *testing.T, raw string) string {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal([]byte(raw), &m); err != nil {
		t.Fatalf("响应不是合法 JSON：%s", raw)
	}
	delete(m, "trace_id")
	delete(m, "detail")
	out, _ := json.Marshal(m)
	return string(out)
}

// 核心用例：另一个租户访问**任何一个**按 job_id 的接口都拿不到东西，
// 且响应与「任务不存在」完全一致。
func TestEveryJobScopedRouteRejectsForeignTenant(t *testing.T) {
	h := newHarness(t)
	jobID := "job-tenant-1"
	job := h.seedTenantJob(t, jobID, tenantA)
	shotID := job.Shots[0].ShotID

	routes := h.jobScopedRoutes()
	if len(routes) < 7 {
		// 反向保护：路由表若因为重构而变空/变小，下面的循环会"全部通过"，
		// 用例变成永真。必须让这种情况红掉。
		t.Fatalf("只发现 %d 条按 job_id 的路由，明显偏少，用例可能已退化为永真：%v", len(routes), routes)
	}
	t.Logf("共 %d 条按 job_id 的路由：%v", len(routes), routes)

	for _, route := range routes {
		t.Run(route, func(t *testing.T) {
			method, path, body := requestFor(route, jobID, shotID)

			// ① 另一个租户：必须 404
			foreign := h.asTenant(method, path, tenantB, body)
			// ② 不存在的任务：作为对照，响应必须与 ① **不可区分**
			missing := h.asTenant(method, strings.ReplaceAll(path, jobID, "job-does-not-exist"), tenantB, body)

			if foreign.Code != http.StatusNotFound {
				t.Fatalf("跨租户访问 %s %s 期望 404，实际 %d：%s", method, path, foreign.Code, foreign.Body.String())
			}
			if got, want := normalizeBody(t, foreign.Body.String()), normalizeBody(t, missing.Body.String()); got != want {
				t.Fatalf("越权与不存在的响应必须不可区分，否则可枚举有效 job_id：\n  越权  = %s\n  不存在= %s", got, want)
			}
			// detail 里若出现「不属于该租户」之类的字样，等于直接确认了 job 存在。
			for _, leak := range []string{"租户", "tenant"} {
				if strings.Contains(strings.ToLower(foreign.Body.String()), strings.ToLower(leak)) {
					t.Fatalf("越权响应泄漏了归属信息（出现 %q）：%s", leak, foreign.Body.String())
				}
			}
		})
	}
}

// 正面对照：本租户访问同一个接口**必须**成功。
//
// 少了这条，一个「所有请求都返回 404」的实现也能让上面的用例全绿 ——
// 那当然是"隔离"了，但服务也不能用了。
func TestOwningTenantStillHasAccess(t *testing.T) {
	h := newHarness(t)
	jobID := "job-tenant-2"
	h.seedTenantJob(t, jobID, tenantA)

	for _, tc := range []struct{ name, path string }{
		{"详情", "/api/v1/jobs/" + jobID},
		{"分镜", "/api/v1/jobs/" + jobID + "/shots"},
		{"事件", "/api/v1/jobs/" + jobID + "/events"},
		{"成本", "/api/v1/jobs/" + jobID + "/cost"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			w := h.asTenant(http.MethodGet, tc.path, tenantA, "")
			if w.Code != http.StatusOK {
				t.Fatalf("本租户访问应当成功，实际 %d：%s", w.Code, w.Body.String())
			}
		})
	}
}

// 归属必须在**创建时**落定，并且能从详情里读回来。
func TestGenerateStampsTenantOnCreate(t *testing.T) {
	h := newHarness(t)
	// HandleGenerate 会先探活 AI 服务；本用例只关心归属落库，
	// 因此直接调 store 之外的最小路径：走真实的 HTTP 接口并接受它可能因
	// AI 不可用而 503 —— 那就跳过，不把「环境里没有 AI 服务」伪装成归属缺陷。
	w := h.asTenant(http.MethodPost, "/api/v1/generate", tenantA,
		`{"raw_script":"这是一个足够长的测试脚本，用于验证创建时会把租户归属写进任务。","target_duration_sec":30}`)
	if w.Code == http.StatusServiceUnavailable {
		t.Skip("AI 服务不可达（HandleGenerate 会先探活），跳过创建时的归属落库验证")
	}
	if w.Code != http.StatusAccepted && w.Code != http.StatusOK {
		t.Fatalf("创建任务失败：%d %s", w.Code, w.Body.String())
	}

	var env struct {
		Data struct {
			JobID string `json:"job_id"`
		} `json:"data"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil {
		t.Fatalf("解析响应失败：%v", err)
	}
	job, err := h.store.GetJob(h.ctx, env.Data.JobID)
	if err != nil {
		t.Fatalf("读取任务失败：%v", err)
	}
	if job.TenantID != tenantA {
		t.Fatalf("任务归属应为 %q，实际 %q", tenantA, job.TenantID)
	}
}

// 缺失租户头时回落到 default —— 单租户部署（本地开发）不该被迫改客户端。
func TestMissingTenantHeaderFallsBackToDefault(t *testing.T) {
	h := newHarness(t)
	jobID := "job-tenant-3"
	h.seedTenantJob(t, jobID, domain.DefaultTenantID)

	req := httptest.NewRequest(http.MethodGet, "/api/v1/jobs/"+jobID, nil)
	w := httptest.NewRecorder()
	h.router.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("无租户头时应当回落到 default 并可访问，实际 %d：%s", w.Code, w.Body.String())
	}
}

// 非法租户头必须被拒绝，而不是被"清洗"成一个合法值。
//
// 静默清洗会让一个配错头的调用方以为自己拿到了正确的归属。
func TestInvalidTenantHeaderIsRejected(t *testing.T) {
	h := newHarness(t)
	for _, bad := range []string{"has space", "中文租户", strings.Repeat("x", 65), "semi;colon"} {
		w := h.asTenant(http.MethodGet, "/api/v1/jobs/whatever", bad, "")
		if w.Code != http.StatusBadRequest {
			t.Fatalf("非法租户 %q 应当 400，实际 %d：%s", bad, w.Code, w.Body.String())
		}
	}
}

// 旧任务（多租户上线前创建，没有 tenant_id）仍应能被 default 租户访问。
//
// 否则部署这一步会让所有历史任务突然变成 404 —— 一次"上线即数据丢失"。
func TestLegacyJobWithoutTenantIsReachableByDefault(t *testing.T) {
	h := newHarness(t)
	jobID := "job-legacy-1"
	// seedJob 不设 TenantID，模拟旧数据。
	h.seedJob(t, jobID, domain.StatusApproved)

	w := h.asTenant(http.MethodGet, "/api/v1/jobs/"+jobID, domain.DefaultTenantID, "")
	if w.Code != http.StatusOK {
		t.Fatalf("无归属的历史任务应可被 default 租户访问，实际 %d：%s", w.Code, w.Body.String())
	}
	// 而其它租户依然读不到它 —— 兼容不等于放开。
	if w2 := h.asTenant(http.MethodGet, "/api/v1/jobs/"+jobID, tenantB, ""); w2.Code != http.StatusNotFound {
		t.Fatalf("历史任务也不该被其它租户读到，实际 %d", w2.Code)
	}
}
