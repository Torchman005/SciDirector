package reconcile

// 对账执行者的用例（阶段五）。
//
// 用**真实 Redis + 真实 gRPC server**：本文件要验证的正是"读两侧、写一侧、发事件"
// 这条链路，用替身会把"到底写进去了什么、发出去了什么"一起假掉。
//
// 最关键的一条是**幂等**：修复会改变用户看到的状态并发出事件，
// 重复执行必须不产生多余事件 —— 周期扫描每 30 秒跑一次，
// 不幂等就会把事件流刷满，并让"修复了几次"这个信息彻底失去意义。

import (
	"context"
	"io"
	"log/slog"
	"net"
	"testing"
	"time"

	goredis "github.com/redis/go-redis/v9"
	"google.golang.org/grpc"

	"github.com/itJinYu/SciDirector/backend/internal/ai"
	"github.com/itJinYu/SciDirector/backend/internal/config"
	"github.com/itJinYu/SciDirector/backend/internal/domain"
	pb "github.com/itJinYu/SciDirector/backend/internal/pb/scidirector/v1"
	"github.com/itJinYu/SciDirector/backend/internal/store"
)

const testRedisDB = 11 // 与其它包错开（worker 13 / httpapi 14 / queue 15）

// fakeCheckpointServer 只实现 GetCheckpointSnapshot。
type fakeCheckpointServer struct {
	pb.UnimplementedAiDirectorServiceServer
	resp *pb.CheckpointSnapshotResponse
	err  error
}

func (f *fakeCheckpointServer) GetCheckpointSnapshot(
	context.Context, *pb.CheckpointSnapshotRequest,
) (*pb.CheckpointSnapshotResponse, error) {
	if f.err != nil {
		return nil, f.err
	}
	return f.resp, nil
}

func newHarness(t *testing.T, fake *fakeCheckpointServer) (*Reconciler, *store.Store, context.Context) {
	t.Helper()

	addr := "127.0.0.1:6379"
	ctx := context.Background()

	rdb := goredis.NewClient(&goredis.Options{Addr: addr, DB: testRedisDB})
	if err := rdb.FlushDB(ctx).Err(); err != nil {
		t.Skipf("Redis 不可达（%s），跳过对账集成测试: %v", addr, err)
	}
	_ = rdb.Close()

	st, err := store.New(ctx, config.RedisConfig{Addr: addr, DB: testRedisDB})
	if err != nil {
		t.Fatalf("构造 Store 失败: %v", err)
	}
	t.Cleanup(func() { _ = st.Close() })

	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("监听 gRPC 端口失败: %v", err)
	}
	gs := grpc.NewServer()
	pb.RegisterAiDirectorServiceServer(gs, fake)
	go func() { _ = gs.Serve(lis) }()
	t.Cleanup(gs.Stop)

	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	aiClient, err := ai.NewClient(config.AIConfig{
		Addr:             lis.Addr().String(),
		UnaryTimeout:     10 * time.Second,
		StreamTimeout:    time.Minute,
		MaxRecvMsgSizeMB: 8,
	}, logger)
	if err != nil {
		t.Fatalf("构造 ai 客户端失败: %v", err)
	}
	t.Cleanup(func() { _ = aiClient.Close() })

	return New(st, aiClient, logger), st, ctx
}

// seedStuckJob 造一个「Redis 非终态、checkpoint 已跑完」的任务 ——
// 也就是"最后一次状态迁移丢失"留下的现场。
func seedStuckJob(t *testing.T, ctx context.Context, st *store.Store) string {
	t.Helper()
	job := &domain.Job{
		JobID:     "job-recon-1",
		TenantID:  "acme",
		Status:    domain.JobRendering,
		CreatedAt: time.Now().UTC(),
		UpdatedAt: time.Now().UTC(),
		Shots: []*domain.Shot{
			{ShotID: "s0", Status: domain.StatusApproved},
			{ShotID: "s1", Status: domain.StatusAwaitingHuman},
		},
	}
	if err := st.SaveJob(ctx, job); err != nil {
		t.Fatalf("写入任务失败: %v", err)
	}
	return job.JobID
}

func finishedPostgresSnapshot() *pb.CheckpointSnapshotResponse {
	return &pb.CheckpointSnapshotResponse{
		Found: true, Finished: true, Cursor: 2, Backend: "postgres", Detail: "已持久化",
		Shots: []*pb.CheckpointShotState{
			{ShotId: "s0", Attempt: 1, HasArtifact: false},
			{ShotId: "s1", Attempt: 1, HasArtifact: false},
		},
	}
}

func TestRepairFixesStuckJobAndEmitsEvent(t *testing.T) {
	r, st, ctx := newHarness(t, &fakeCheckpointServer{resp: finishedPostgresSnapshot()})
	jobID := seedStuckJob(t, ctx, st)

	out, err := r.ReconcileJob(ctx, jobID, true)
	if err != nil {
		t.Fatalf("对账失败: %v", err)
	}
	if !out.Repaired {
		t.Fatalf("应当执行修复，实际 %+v", out.Report.Divergences)
	}
	// 有一个镜头 AWAITING_HUMAN ⇒ 终态应是 PARTIAL（与 worker 收尾判断一致）。
	if out.RepairedTo != domain.JobPartial {
		t.Fatalf("修复后的状态应为 PARTIAL，实际 %s", out.RepairedTo)
	}

	job, err := st.GetJob(ctx, jobID)
	if err != nil {
		t.Fatalf("读取任务失败: %v", err)
	}
	if job.Status != domain.JobPartial {
		t.Fatalf("任务状态未被修复：%s", job.Status)
	}

	// 必须写事件：不写的话接口读出来已经结束、而前端还停在 100% 转圈。
	events, err := st.ListEvents(ctx, jobID, 0)
	if err != nil {
		t.Fatalf("读取事件失败: %v", err)
	}
	var found bool
	for _, e := range events {
		if e.Node == "reconcile" {
			found = true
			if e.Payload["reason"] != string(domain.DivStuckJob) {
				t.Fatalf("事件缺少 reason: %+v", e.Payload)
			}
			if e.Payload["from"] != string(domain.JobRendering) || e.Payload["to"] != string(domain.JobPartial) {
				t.Fatalf("事件缺少 from/to: %+v", e.Payload)
			}
		}
	}
	if !found {
		t.Fatal("修复必须写一条 reconcile 事件（否则前端不会更新）")
	}
}

// 只读模式：报差异但**不动**状态、不发事件。
func TestReadOnlyReconcileDoesNotTouchState(t *testing.T) {
	r, st, ctx := newHarness(t, &fakeCheckpointServer{resp: finishedPostgresSnapshot()})
	jobID := seedStuckJob(t, ctx, st)

	out, err := r.ReconcileJob(ctx, jobID, false)
	if err != nil {
		t.Fatalf("对账失败: %v", err)
	}
	if out.Repaired {
		t.Fatal("只读模式不该修复")
	}
	if out.Report.Consistent() {
		t.Fatal("只读模式仍应报出差异")
	}
	job, _ := st.GetJob(ctx, jobID)
	if job.Status != domain.JobRendering {
		t.Fatalf("只读模式不该改变状态，实际 %s", job.Status)
	}
	events, _ := st.ListEvents(ctx, jobID, 0)
	for _, e := range events {
		if e.Node == "reconcile" {
			t.Fatal("只读模式不该写事件")
		}
	}
}

// 幂等：重复修复不产生多余事件。
//
// 周期扫描每 30 秒跑一次；不幂等会把事件流刷满，
// 并让「修复了几次」这个信息彻底失去意义。
func TestRepairIsIdempotent(t *testing.T) {
	r, st, ctx := newHarness(t, &fakeCheckpointServer{resp: finishedPostgresSnapshot()})
	jobID := seedStuckJob(t, ctx, st)

	for i := 0; i < 3; i++ {
		if _, err := r.ReconcileJob(ctx, jobID, true); err != nil {
			t.Fatalf("第 %d 次对账失败: %v", i+1, err)
		}
	}

	events, _ := st.ListEvents(ctx, jobID, 0)
	n := 0
	for _, e := range events {
		if e.Node == "reconcile" {
			n++
		}
	}
	if n != 1 {
		t.Fatalf("三次修复应只产生 1 条事件（第二次起任务已终态，二次确认会跳过），实际 %d", n)
	}
}

// 非持久后端：报 unknown、不修。
//
// 内存 checkpoint 进程重启后什么都没有，found=false 是**预期**行为。
// 把它当差异并"修复"，会让开发环境里每个在跑的任务都被误改状态。
func TestNonDurableBackendIsNotRepaired(t *testing.T) {
	r, st, ctx := newHarness(t, &fakeCheckpointServer{resp: &pb.CheckpointSnapshotResponse{
		Found: false, Backend: "memory", Detail: "仅进程内有效",
	}})
	jobID := seedStuckJob(t, ctx, st)

	out, err := r.ReconcileJob(ctx, jobID, true)
	if err != nil {
		t.Fatalf("对账失败: %v", err)
	}
	if out.Repaired {
		t.Fatal("非持久后端不该触发修复")
	}
	if len(out.Report.Divergences) != 1 || out.Report.Divergences[0].Kind != domain.DivUnknown {
		t.Fatalf("应只报一条 unknown，实际 %+v", out.Report.Divergences)
	}
	job, _ := st.GetJob(ctx, jobID)
	if job.Status != domain.JobRendering {
		t.Fatalf("状态不该被改动，实际 %s", job.Status)
	}
}

// Python 侧不可达时必须**返回错误**，而不是当成"没有差异"。
//
// 把读失败当成一致，会让对账在 AI 服务挂掉的时候永远显示"一切正常" ——
// 那正是最需要它工作的时刻。
func TestCheckpointReadFailureSurfacesAsError(t *testing.T) {
	r, st, ctx := newHarness(t, &fakeCheckpointServer{err: context.DeadlineExceeded})
	jobID := seedStuckJob(t, ctx, st)

	out, err := r.ReconcileJob(ctx, jobID, true)
	if err == nil {
		t.Fatalf("读取失败时应当返回错误，实际返回 %+v", out)
	}
	if out != nil {
		t.Fatalf("失败时不该返回结果：%+v", out)
	}
}

// 任务不存在：返回 store 的错误（上层映射为 404）。
func TestReconcileMissingJobReturnsNotFound(t *testing.T) {
	r, _, ctx := newHarness(t, &fakeCheckpointServer{resp: finishedPostgresSnapshot()})
	if _, err := r.ReconcileJob(ctx, "job-does-not-exist", true); err == nil {
		t.Fatal("不存在的任务应返回错误")
	}
}
