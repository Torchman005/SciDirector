package domain

import (
	"testing"
)

// TestTransitionLegal 覆盖状态机的合法迁移。
// 状态机是正确性核心，必须逐个枚举验证，防止后续重构悄悄改坏。
func TestTransitionLegal(t *testing.T) {
	legal := []struct{ from, to ShotStatus }{
		{StatusPending, StatusGenerating},
		{StatusGenerating, StatusRendering},
		{StatusRendering, StatusCritiquing},
		{StatusCritiquing, StatusApproved},
		{StatusCritiquing, StatusRejected},
		{StatusRejected, StatusRetrying},
		{StatusRetrying, StatusGenerating},
		{StatusCritiquing, StatusAwaitingHuman},
		{StatusRetrying, StatusAwaitingHuman},
		{StatusAwaitingHuman, StatusRetrying},
		{StatusAwaitingHuman, StatusApproved},
		{StatusApproved, StatusRetrying},  // HITL：人工要求重做已通过的镜头
		{StatusRendering, StatusRetrying}, // 渲染编译失败 -> 带错误回灌重写
	}
	for _, c := range legal {
		if _, err := Transition(c.from, c.to); err != nil {
			t.Errorf("期望 %s -> %s 合法，却报错: %v", c.from, c.to, err)
		}
	}
}

// TestTransitionIllegal 覆盖必须被拒绝的迁移。
// 尤其要保证：不能从 PENDING 直接跳到 APPROVED，否则「未经渲染即通过」的 bug 会被掩盖。
func TestTransitionIllegal(t *testing.T) {
	illegal := []struct{ from, to ShotStatus }{
		{StatusPending, StatusApproved},
		{StatusPending, StatusCritiquing},
		{StatusGenerating, StatusApproved},
		{StatusApproved, StatusRejected},
		{StatusApproved, StatusPending},
	}
	for _, c := range illegal {
		if _, err := Transition(c.from, c.to); err == nil {
			t.Errorf("期望 %s -> %s 非法，却通过了", c.from, c.to)
		}
	}
}

// TestTransitionIdempotent 保证重复写入同一状态被视为合法（事件重放场景必需）。
func TestTransitionIdempotent(t *testing.T) {
	for _, s := range AllShotStatuses {
		if _, err := Transition(s, s); err != nil {
			t.Errorf("同状态写入 %s -> %s 应当幂等通过，却报错: %v", s, s, err)
		}
	}
}

// TestEngineForTag 验证「标签 -> 引擎」的确定性路由，且未登记标签必须报错。
func TestEngineForTag(t *testing.T) {
	cases := map[Tag]Engine{
		TagMath:     EngineManim,
		TagData:     EngineD3,
		TagCode:     EngineCodeAnim,
		TagAmbience: EngineStock,
	}
	for tag, want := range cases {
		got, err := EngineForTag(tag)
		if err != nil {
			t.Fatalf("标签 %s 路由失败: %v", tag, err)
		}
		if got != want {
			t.Errorf("标签 %s 期望引擎 %s，实际 %s", tag, want, got)
		}
	}
	if _, err := EngineForTag(Tag("UNKNOWN")); err == nil {
		t.Error("未知标签应当返回错误，而不是静默路由")
	}
}

// TestMaxAttemptsExceeded 验证熔断阈值边界。
func TestMaxAttemptsExceeded(t *testing.T) {
	if MaxAttemptsExceeded(2, 3) {
		t.Error("attempt=2 < max=3 不应触发熔断")
	}
	if !MaxAttemptsExceeded(3, 3) {
		t.Error("attempt=3 >= max=3 应触发熔断")
	}
	if !MaxAttemptsExceeded(5, 3) {
		t.Error("attempt=5 > max=3 应触发熔断")
	}
}

// TestShotIDStable 保证镜头 ID 由 jobID + index 稳定派生（人工反馈依赖这一稳定性）。
func TestShotIDStable(t *testing.T) {
	got := ShotID("job-abc", 7)
	if want := "job-abc-s007"; got != want {
		t.Errorf("ShotID 期望 %q，实际 %q", want, got)
	}
	if ShotID("job-abc", 7) != got {
		t.Error("ShotID 必须是确定性的")
	}
}

// TestJobProgress 验证进度计算：只有 APPROVED 计入完成。
func TestJobProgress(t *testing.T) {
	j := &Job{Shots: []*Shot{
		{Status: StatusApproved},
		{Status: StatusRendering},
		{Status: StatusAwaitingHuman},
		{Status: StatusFailed},
	}}
	if got := j.ProgressRatio(); got != 0.25 {
		t.Errorf("进度期望 0.25，实际 %v", got)
	}
	st := j.Stat()
	if st.Total != 4 || st.Approved != 1 || st.Failed != 1 || st.AwaitingHuman != 1 || st.InProgress != 1 {
		t.Errorf("统计结果不符合预期: %+v", st)
	}
}
