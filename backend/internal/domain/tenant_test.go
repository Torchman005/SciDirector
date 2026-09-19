package domain

// 配额的**判定逻辑**用例（阶段五·多租户）。
//
// 判定被做成纯函数正是为了能这样测：配额算错的表现是「合法请求被随机拒绝」，
// 排查时很难从线上现象反推到某一行的比较符。

import (
	"errors"
	"testing"
)

func TestCheckQuotaUnlimitedWhenLimitIsZero(t *testing.T) {
	// 缺省不限制是刻意的：单租户/本地开发不该被凭空出现的上限挡住。
	// 用 0 与负数都试，避免实现里写成 `limit == 0` 而漏掉负数。
	for _, limit := range []int{0, -1} {
		if err := CheckQuota("t", 9999, limit); err != nil {
			t.Fatalf("limit=%d 应当不限制，实际 %v", limit, err)
		}
	}
}

func TestCheckQuotaBoundary(t *testing.T) {
	// 边界是 >= 而不是 >：limit=2 时应答「最多 2 个在跑」，
	// 已经跑着 2 个就不能再放第 3 个。写成 > 会让实际并发比配置多 1，
	// 而这种偏差在小配额下就是 50%。
	if err := CheckQuota("t", 1, 2); err != nil {
		t.Fatalf("1/2 应当放行，实际 %v", err)
	}
	err := CheckQuota("t", 2, 2)
	if err == nil {
		t.Fatal("2/2 应当拒绝")
	}
	var q *QuotaExceededError
	if !errors.As(err, &q) {
		t.Fatalf("应当是 *QuotaExceededError，实际 %T", err)
	}
	// 数字要如实带上：消息里没有确切数字时，用户不知道要等多久、该找谁提额。
	if q.Active != 2 || q.Limit != 2 || q.TenantID != "t" {
		t.Fatalf("配额错误里的数字不对: %+v", q)
	}
}

// 「在跑」的语义：等人工的**不占配额**。
//
// 任务层面「有镜头转人工」体现为 PARTIAL（终态），因此 PARTIAL 必须返回 false：
// 否则一个卡着人工审核的任务会把整个租户挡在门外，
// 而那恰恰是最需要用户还能继续提交别的任务的时候。
func TestJobActiveExcludesTerminalAndHumanReview(t *testing.T) {
	cases := map[JobStatus]bool{
		JobCompleted: false,
		JobPartial:   false,
		JobFailed:    false,
		JobCreated:   true,
		JobPlanning:  true,
		JobRendering: true,
		JobComposing: true,
	}
	for status, want := range cases {
		if got := JobActive(status); got != want {
			t.Fatalf("JobActive(%s) = %v，期望 %v", status, got, want)
		}
	}
}

func TestNormalizeTenantID(t *testing.T) {
	ok := map[string]string{
		"":           DefaultTenantID, // 空 = 回落缺省
		"   ":        DefaultTenantID, // 只有空白也算空
		"acme":       "acme",
		" acme ":     "acme", // 去空白
		"team-1_a.b": "team-1_a.b",
	}
	for raw, want := range ok {
		got, valid := NormalizeTenantID(raw)
		if !valid || got != want {
			t.Fatalf("NormalizeTenantID(%q) = (%q,%v)，期望 (%q,true)", raw, got, valid, want)
		}
	}

	// 非法值必须**拒绝**而不是清洗：静默清洗会让配错头的调用方
	// 以为自己拿到了正确的归属。
	for _, bad := range []string{"has space", "中文", "semi;colon", "sl/ash", string(make([]byte, 65))} {
		if _, valid := NormalizeTenantID(bad); valid {
			t.Fatalf("非法租户 %q 应当被拒绝", bad)
		}
	}
}

func TestJobBelongsToTreatsEmptyAsDefault(t *testing.T) {
	legacy := &Job{JobID: "j1"} // 多租户上线前的旧任务：没有归属字段
	if !JobBelongsTo(legacy, DefaultTenantID) {
		t.Fatal("旧任务应当可被 default 租户访问，否则上线即数据丢失")
	}
	if !JobBelongsTo(legacy, "") {
		t.Fatal("空租户参数应视为 default")
	}
	if JobBelongsTo(legacy, "acme") {
		t.Fatal("兼容不等于放开：其它租户不该读到旧任务")
	}
	if JobBelongsTo(nil, DefaultTenantID) {
		t.Fatal("nil 任务不该算属于任何人")
	}
}
