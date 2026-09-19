package httpapi

// 配额与在跑集合的**真实 Redis** 用例（阶段五·多租户）。
//
// 用真实 Redis 的理由：这里测的是「集合随真实任务状态自愈」这个性质 ——
// 它在内存替身上根本无法体现（替身里没有"任务的真实状态"这回事）。
//
// 最关键的一条是自愈：计数器实现（创建 +1、结束 -1）在 worker 崩溃、
// 任务被手工清理、进程重启之后会永远偏高，表现是「这个租户再也提交不了任务」
// 且没有任何日志能说明原因。本文件用「先造脏数据、再检查是否被修正」把它钉住。

import (
	"strings"
	"testing"
	"time"

	goredis "github.com/redis/go-redis/v9"

	"github.com/itJinYu/SciDirector/backend/internal/domain"
)

// seedActive 把一个任务记进租户的在跑集合（模拟"曾经创建过"）。
func (h *harness) seedActive(t *testing.T, tenantID, jobID string, status domain.JobStatus) {
	t.Helper()
	// seedJob 的入参是**镜头**状态，这里要的是**任务**状态 —— 两者是不同类型，
	// 别把镜头状态直接塞进 Job.Status（编译期就会拦下来，这正是强类型的好处）。
	job := h.seedJob(t, jobID, domain.StatusApproved)
	job.TenantID = tenantID
	job.Status = status
	if err := h.store.SaveJob(h.ctx, job); err != nil {
		t.Fatalf("写入任务失败: %v", err)
	}
	if err := h.store.TrackActiveJob(h.ctx, tenantID, jobID, time.Now().UTC()); err != nil {
		t.Fatalf("登记在跑任务失败: %v", err)
	}
}

func TestCountActiveJobsIgnoresTerminalJobs(t *testing.T) {
	h := newHarness(t)
	const tenant = "quota-a"

	h.seedActive(t, tenant, "job-q-running", domain.JobRendering)
	h.seedActive(t, tenant, "job-q-partial", domain.JobPartial)
	h.seedActive(t, tenant, "job-q-done", domain.JobCompleted)
	h.seedActive(t, tenant, "job-q-failed", domain.JobFailed)

	got, err := h.store.CountActiveJobs(h.ctx, tenant)
	if err != nil {
		t.Fatalf("统计失败: %v", err)
	}
	// 只有 RENDERING 那个算「在跑」。PARTIAL 也在其中：它已经停了、在等人类。
	if got != 1 {
		t.Fatalf("在跑任务数应为 1（只有 RENDERING），实际 %d", got)
	}
}

// 自愈：集合里残留了「任务已经被删掉」的条目时，下一次统计必须把它剔除。
//
// 这正是计数器实现做不到的事 —— 也是本设计存在的理由。
func TestCountActiveJobsSelfHealsAfterJobRemoval(t *testing.T) {
	h := newHarness(t)
	const tenant = "quota-b"

	h.seedActive(t, tenant, "job-q-ghost", domain.JobRendering)
	if got, _ := h.store.CountActiveJobs(h.ctx, tenant); got != 1 {
		t.Fatalf("前置条件不成立：应为 1，实际 %d", got)
	}

	// 模拟任务被清理掉（集合里仍有条目）。
	//
	// 任务本身带 7 天 TTL，所以「集合里还留着、任务已经过期消失」是**真实存在**的场景，
	// 不是构造出来的极端情况。这里直接从 Redis 删键来复现它 ——
	// 不为测试往生产代码里加一个没有调用方的 DeleteJob。
	rdb := goredis.NewClient(&goredis.Options{Addr: testRedisAddr(t), DB: 14})
	t.Cleanup(func() { _ = rdb.Close() })
	if err := rdb.Del(h.ctx, "scid:job:"+"job-q-ghost").Err(); err != nil {
		t.Fatalf("删除任务键失败: %v", err)
	}

	got, err := h.store.CountActiveJobs(h.ctx, tenant)
	if err != nil {
		t.Fatalf("统计失败: %v", err)
	}
	if got != 0 {
		t.Fatalf("任务已不存在时不该继续占用配额（否则租户会被永久挡住），实际 %d", got)
	}
}

// 租户之间互不影响：一个租户占满配额，不该影响另一个。
//
// 少了这条，一个「全局计数器」的实现也能通过上面两个用例 ——
// 那不叫多租户配额，只是换了名字的全局上限。
func TestQuotaIsPerTenant(t *testing.T) {
	h := newHarness(t)

	for i, id := range []string{"job-q-a1", "job-q-a2", "job-q-a3"} {
		h.seedActive(t, "quota-heavy", id, domain.JobRendering)
		_ = i
	}
	h.seedActive(t, "quota-light", "job-q-b1", domain.JobRendering)

	heavy, err := h.store.CountActiveJobs(h.ctx, "quota-heavy")
	if err != nil {
		t.Fatalf("统计失败: %v", err)
	}
	light, err := h.store.CountActiveJobs(h.ctx, "quota-light")
	if err != nil {
		t.Fatalf("统计失败: %v", err)
	}
	if heavy != 3 || light != 1 {
		t.Fatalf("两个租户的在跑数应各自独立（3 与 1），实际 %d 与 %d", heavy, light)
	}
}

// 登记失败不该让任务创建失败。
//
// 配额是保护性能力：它自己出问题时正确的行为是「这次不计数」，
// 而不是让用户提交不了任务。这里直接用空 jobID 触发 early-return 分支，
// 确认 TrackActiveJob 不会因此报错（该分支是刻意放行的）。
func TestTrackActiveJobIsLenientOnEmptyInput(t *testing.T) {
	h := newHarness(t)
	if err := h.store.TrackActiveJob(h.ctx, "", "", time.Now().UTC()); err != nil {
		t.Fatalf("空输入应当静默放行，实际 %v", err)
	}
	if err := h.store.TrackActiveJob(h.ctx, "t", "", time.Now().UTC()); err != nil {
		t.Fatalf("空 jobID 应当静默放行，实际 %v", err)
	}
}

// 端到端：配额用满时 POST /generate 必须返回 **429**（而不是 400/500）。
//
// 状态码是契约的一部分：客户端据此区分「请求写错了」（重试无用）
// 与「现在不行，等会儿再来」（应当退避重试）。返回 500 会让客户端把
// 一个正常的限流当成服务端 bug 上报，返回 400 则会让人去改请求体。
func TestGenerateReturns429WhenQuotaExhausted(t *testing.T) {
	h := newHarnessWithQuota(t, 2)
	const tenant = "quota-tenant"

	h.seedActive(t, tenant, "job-q-e1", domain.JobRendering)
	h.seedActive(t, tenant, "job-q-e2", domain.JobRendering)

	w := h.asTenant("POST", "/api/v1/generate", tenant,
		`{"raw_script":"这是一个足够长的测试脚本，用于验证配额用满时会拒绝新的生成任务请求。","target_duration_sec":30}`)

	// HandleGenerate 会先探活 AI；本用例只关心配额分支，环境里没有 AI 服务时跳过。
	if w.Code == 503 {
		t.Skip("AI 服务不可达（HandleGenerate 会先探活），跳过配额端到端验证")
	}
	if w.Code != 429 {
		t.Fatalf("配额用满时应返回 429，实际 %d：%s", w.Code, w.Body.String())
	}
	// 错误码也要是 RATE_LIMITED，客户端据此做退避。
	if !strings.Contains(w.Body.String(), "RATE_LIMITED") {
		t.Fatalf("错误码应为 RATE_LIMITED，实际：%s", w.Body.String())
	}
	// 消息里要有确切数字，否则用户不知道要等多久。
	if !strings.Contains(w.Body.String(), "2/2") {
		t.Fatalf("拒绝消息里应给出确切数字（2/2），实际：%s", w.Body.String())
	}
}

// 反向控制：配额**没**用满时必须放行到下一步（而不是一律 429）。
//
// 少了这条，一个「永远返回 429」的实现也能让上面的用例通过 ——
// 那确实"限流"了，但服务也不能用了。
func TestGenerateIsNotRejectedWhenQuotaHasRoom(t *testing.T) {
	h := newHarnessWithQuota(t, 3)
	const tenant = "quota-tenant-room"

	h.seedActive(t, tenant, "job-q-r1", domain.JobRendering)

	w := h.asTenant("POST", "/api/v1/generate", tenant,
		`{"raw_script":"这是一个足够长的测试脚本，用于验证配额有余量时不会误拒新的生成任务。","target_duration_sec":30}`)
	if w.Code == 503 {
		t.Skip("AI 服务不可达，跳过该验证")
	}
	if w.Code == 429 {
		t.Fatalf("配额有余量（1/3）时不该拒绝：%s", w.Body.String())
	}
}
