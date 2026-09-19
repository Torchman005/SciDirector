package domain

// 状态对账的**判断规则**用例（阶段五）。
//
// 这些规则必须被完整单测：它们一旦算错，表现是「对账报告全是误报」，
// 而误报会让人干脆不再看这份报告 —— 那比没有对账更糟。
// 因此重点在**反向控制**：不仅断言「该报的报了」，也断言「不该报的没报」。

import "testing"

// baseJob 造一个有两镜头的在跑任务。
func baseJob() *Job {
	return &Job{
		JobID:  "job-r1",
		Status: JobRendering,
		Shots: []*Shot{
			{ShotID: "s0", Status: StatusApproved, Artifact: &Artifact{VideoPath: "/v0.mp4"}},
			{ShotID: "s1", Status: StatusGenerating},
		},
	}
}

func durableFacts() CheckpointFacts {
	return CheckpointFacts{
		Durable: true,
		Backend: "postgres",
		Found:   true,
		Shots: map[string]CheckpointShotFacts{
			"s0": {Attempt: 1, HasArtifact: true},
			"s1": {Attempt: 1, HasArtifact: false},
		},
	}
}

func TestReconcileConsistentWhenBothSidesAgree(t *testing.T) {
	job := baseJob()
	report := Reconcile(job, durableFacts(), job.Status)

	if !report.Consistent() {
		t.Fatalf("两侧一致时不该报差异：%+v", report.Divergences)
	}
	if len(report.Divergences) != 0 {
		t.Fatalf("一致时差异列表应为空（连 unknown 都不该有），实际 %+v", report.Divergences)
	}
}

// 最典型、也是唯一会自动修的一类：图跑完了，Redis 还停在非终态。
func TestReconcileDetectsStuckJobAndMarksRepairable(t *testing.T) {
	job := baseJob()
	facts := durableFacts()
	facts.Finished = true

	report := Reconcile(job, facts, job.Status)
	if report.Consistent() {
		t.Fatal("应当报出 stuck_job")
	}
	d := report.Divergences[0]
	if d.Kind != DivStuckJob {
		t.Fatalf("种类应为 %s，实际 %s", DivStuckJob, d.Kind)
	}
	if !d.Repairable {
		t.Fatal("stuck_job 是唯一可安全自动修的一类，必须标为可修")
	}
}

// 反向控制：Redis 已是终态时，即使 checkpoint 说没跑完也**不该**报 stuck_job ——
// 报错的种类会导致错误的修复方向（把已结束的任务拉回进行中）。
func TestReconcileDoesNotReportStuckForTerminalJob(t *testing.T) {
	job := baseJob()
	job.Status = JobCompleted
	facts := durableFacts()
	facts.Finished = true

	report := Reconcile(job, facts, job.Status)
	for _, d := range report.Divergences {
		if d.Kind == DivStuckJob {
			t.Fatalf("已终态的任务不该报 stuck_job：%+v", d)
		}
	}
}

// 反向控制：Redis 终态而 checkpoint 未完成 —— 单独一类且**不可修**。
// 图可能继续推进并发出新事件，把 Redis 拉回非终态是危险的
// （用户可能已经看到"已完成"）。
func TestReconcileDetectsCheckpointAheadButNotRepairable(t *testing.T) {
	job := baseJob()
	job.Status = JobCompleted
	facts := durableFacts()
	facts.Finished = false

	report := Reconcile(job, facts, job.Status)
	if report.Consistent() {
		t.Fatal("应当报出 checkpoint_ahead")
	}
	if report.Divergences[0].Kind != DivCheckpointAhead {
		t.Fatalf("种类应为 %s，实际 %s", DivCheckpointAhead, report.Divergences[0].Kind)
	}
	if report.Divergences[0].Repairable {
		t.Fatal("checkpoint_ahead 不该标为可修 —— 自动把终态拉回进行中比不修更危险")
	}
}

// 非持久后端：报 unknown 而**不是**一堆假差异。
//
// 内存 checkpoint 进程重启后什么都没有，那时 found=false 是预期行为。
// 把它算成不一致，会让对账报告在本地开发环境里永远是红的 ——
// 而"永远红的报告"等于没有报告。
func TestReconcileReportsUnknownForNonDurableBackend(t *testing.T) {
	job := baseJob()
	facts := CheckpointFacts{Backend: "memory", Durable: false, Found: false, Detail: "仅进程内有效"}

	report := Reconcile(job, facts, job.Status)
	if len(report.Divergences) != 1 || report.Divergences[0].Kind != DivUnknown {
		t.Fatalf("非持久后端应只报一条 unknown，实际 %+v", report.Divergences)
	}
	// Consistent() 应当把 unknown 视为"没有差异"：它不是不一致，而是无从比较。
	if !report.Consistent() {
		t.Fatal("unknown 不该让报告变成 inconsistent")
	}
	// 报告必须能自我解释：读报告的人不该还得去翻服务日志才知道为什么是 unknown。
	if report.Divergences[0].Message == "" || report.CheckpointBackend != "memory" {
		t.Fatalf("unknown 必须带上后端与原因：%+v", report)
	}
}

// Redis 在跑、checkpoint 里没有该线程：只报告、不自动修。
// 「尚未开始」与「checkpoint 被清理」含义完全不同，需要结合任务年龄判断。
func TestReconcileReportsMissingCheckpointWithoutRepair(t *testing.T) {
	job := baseJob()
	facts := CheckpointFacts{Durable: true, Backend: "postgres", Found: false}

	report := Reconcile(job, facts, job.Status)
	if len(report.Divergences) != 1 || report.Divergences[0].Kind != DivCheckpointMissing {
		t.Fatalf("应报 checkpoint_missing，实际 %+v", report.Divergences)
	}
	if report.Divergences[0].Repairable {
		t.Fatal("不该自动修：可能只是任务还没开始")
	}
}

// 反向控制：任务已终态且 checkpoint 也没了 —— 正常（可能已过保留期），不报差异。
func TestReconcileQuietWhenTerminalJobHasNoCheckpoint(t *testing.T) {
	job := baseJob()
	job.Status = JobFailed
	facts := CheckpointFacts{Durable: true, Backend: "postgres", Found: false}

	if report := Reconcile(job, facts, job.Status); !report.Consistent() {
		t.Fatalf("终态任务没有 checkpoint 是正常的，不该报差异：%+v", report.Divergences)
	}
}

func TestReconcileReportsArtifactMismatchPerShot(t *testing.T) {
	job := baseJob()
	facts := durableFacts()
	// checkpoint 说 s1 有产物，Redis 里没有（事务性差异）。
	facts.Shots["s1"] = CheckpointShotFacts{Attempt: 1, HasArtifact: true}

	report := Reconcile(job, facts, job.Status)
	found := false
	for _, d := range report.Divergences {
		if d.Kind == DivArtifactOnOneSide && d.ShotID == "s1" {
			found = true
			if d.Repairable {
				t.Fatal("产物差异不该自动修：两侧「产物」的含义不同")
			}
		}
	}
	if !found {
		t.Fatalf("应当报出 s1 的产物差异：%+v", report.Divergences)
	}
}

func TestReconcileReportsShotUnknownToRedis(t *testing.T) {
	job := baseJob()
	facts := durableFacts()
	facts.Shots["s9"] = CheckpointShotFacts{Attempt: 1}

	report := Reconcile(job, facts, job.Status)
	found := false
	for _, d := range report.Divergences {
		if d.Kind == DivShotUnknownToRedis && d.ShotID == "s9" {
			found = true
		}
	}
	if !found {
		t.Fatalf("应当报出 Redis 不知道的镜头：%+v", report.Divergences)
	}
}

func TestReconcileReportsProgressAhead(t *testing.T) {
	job := baseJob()
	facts := durableFacts()
	facts.Cursor = 5 // 远超 Redis 的 2 个镜头

	report := Reconcile(job, facts, job.Status)
	found := false
	for _, d := range report.Divergences {
		if d.Kind == DivProgressAhead {
			found = true
		}
	}
	if !found {
		t.Fatalf("游标超过镜头数时应当报警：%+v", report.Divergences)
	}
}

// nil 任务不该 panic：对账是对着一个可能已被清理的任务跑的。
func TestReconcileToleratesNilJob(t *testing.T) {
	if report := Reconcile(nil, durableFacts(), JobRendering); !report.Consistent() {
		t.Fatalf("nil 任务应返回空报告，实际 %+v", report.Divergences)
	}
}

// ---------------------------------------------------------------------------
// 终态推导（自动修复的唯一依据）
// ---------------------------------------------------------------------------

// 修复只依据 **Redis 自己的镜头状态** 推出终态 ——
// 不引入 checkpoint 侧的信息去改写对外状态。
func TestTerminalStatusFromShots(t *testing.T) {
	cases := []struct {
		name  string
		shots []*Shot
		want  JobStatus
	}{
		{"全部通过 -> COMPLETED", []*Shot{
			{Status: StatusApproved}, {Status: StatusApproved},
		}, JobCompleted},
		{"有镜头转人工 -> PARTIAL", []*Shot{
			{Status: StatusApproved}, {Status: StatusAwaitingHuman},
		}, JobPartial},
		{"还有镜头没通过 -> FAILED", []*Shot{
			{Status: StatusApproved}, {Status: StatusGenerating},
		}, JobFailed},
		{"没有镜头 -> FAILED", nil, JobFailed},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := TerminalStatusFromShots(&Job{Shots: tc.shots})
			if got != tc.want {
				t.Fatalf("期望 %s，实际 %s", tc.want, got)
			}
		})
	}
}

func TestTerminalStatusFromShotsToleratesNilJob(t *testing.T) {
	if got := TerminalStatusFromShots(nil); got != JobFailed {
		t.Fatalf("nil 任务应推出 FAILED，实际 %s", got)
	}
}
