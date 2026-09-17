package worker

// 本文件落地**阶段三验收 B4**：「同一 `(job, shot, attempt)` 重复投递只渲染一次」。
//
// 为什么必须真跑而不是只做单测：
// 幂等闸门保护的是**真金白银的渲染**。它失效时的表现不是报错，而是同一次尝试被渲染两遍
// —— 账单翻倍、且两条流水线并发写同一个镜头目录，事后从日志里极难看出是哪一次多跑了。
// 因此这里不去 mock 掉依赖，而是用**真实的 gRPC server + 真实 Redis** 把整条路径跑通：
// 假 Python 大脑只负责数「ReviseShot 被调用了几次」，这也是 B4 唯一的判定依据。
//
// B4 其实有**两道**闸门，二者防的不是同一件事，缺一不可：
//  1. 入队侧：`queue.Client.EnqueueRenderShot` 用 `asynq.Unique` + `asynq.TaskID` 保证
//     同一次 attempt 只进队列一份（防「人工连点两下」）。见 `queue/enqueue_dedup_test.go`。
//  2. 执行侧：`HandleRenderShot` 的 `AcquireIdempotencyKey`（本文件）。
//     它防的是 Asynq 自身的重复投递：任务**执行成功但确认失败**时，
//     Asynq 会认为它没跑完并再次投递，此时入队侧的去重完全帮不上忙。
//
// 覆盖范围：
//   - 并发重复投递（第一次仍在渲染中时第二次到达）只渲染一次
//   - 完成之后的再次投递同样不重复渲染（幂等占位靠 TTL 兜住）
//   - 不同 attempt 必须各自渲染（否则「总是跳过」这种退化实现也能骗过测试）
//   - 真正失败时**释放**占位，允许后续重试
//   - 能力未实现（UNIMPLEMENTED）时不重试，但占位同样要释放

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net"
	"os"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/hibiken/asynq"
	goredis "github.com/redis/go-redis/v9"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/itJinYu/SciDirector/backend/internal/ai"
	"github.com/itJinYu/SciDirector/backend/internal/archive"
	"github.com/itJinYu/SciDirector/backend/internal/config"
	"github.com/itJinYu/SciDirector/backend/internal/domain"
	"github.com/itJinYu/SciDirector/backend/internal/media"
	pb "github.com/itJinYu/SciDirector/backend/internal/pb/scidirector/v1"
	"github.com/itJinYu/SciDirector/backend/internal/queue"
	"github.com/itJinYu/SciDirector/backend/internal/store"
)

// testRedisDB 是本包集成测试独占的库号。
//
// 必须与 httpapi（14）、queue（15）错开：`go test ./internal/...` 会让**不同包并行**，
// 共用一个库时一边的 FlushDB 会清掉另一边正在断言的键，表现为**随机**失败的假阳性。
// 这类「偶尔红一次」的测试比没有测试更糟，因为它会训练人忽视红灯。
const testRedisDB = 13

// ---------------------------------------------------------------------------
// 假 Python 大脑（真实的 gRPC server）
// ---------------------------------------------------------------------------

// fakeAIServer 只实现 ReviseShot，并数出它被调用了几次。
//
// 用真实的 gRPC 通道而不是接口替身，是为了连带覆盖 `ai.Client` 的错误映射：
// 幂等闸门必须与「错误如何分类」一起工作才算数（可重试 vs 不可重试）。
type fakeAIServer struct {
	pb.UnimplementedAiDirectorServiceServer

	reviseCalls atomic.Int32
	// holdFor 模拟一次真实渲染的耗时，让「第一次还在跑、第二次就到了」这个窗口真实存在。
	holdFor time.Duration
	// failFirst 指定前 N 次调用返回 failCode，用于验证失败路径的占位释放。
	failFirst int32
	failCode  codes.Code
	// entered 在第一次调用进入时关闭，供测试做确定性同步（不靠 sleep 赌时序）。
	entered chan struct{}
	once    sync.Once
}

func (f *fakeAIServer) ReviseShot(ctx context.Context, req *pb.ReviseShotRequest) (*pb.ReviseShotResponse, error) {
	n := f.reviseCalls.Add(1)
	if f.entered != nil {
		f.once.Do(func() { close(f.entered) })
	}

	if f.holdFor > 0 {
		select {
		case <-time.After(f.holdFor):
		case <-ctx.Done():
			return nil, status.Error(codes.Canceled, ctx.Err().Error())
		}
	}

	if n <= f.failFirst {
		return nil, status.Error(f.failCode, "模拟 AI 侧失败")
	}

	shot := req.GetShot()
	return &pb.ReviseShotResponse{
		Success: true,
		Shot:    &pb.ShotSpec{ShotId: shot.GetShotId(), Index: shot.GetIndex()},
		Artifact: &pb.RenderArtifact{
			ArtifactId:  "art-" + shot.GetShotId(),
			ShotId:      shot.GetShotId(),
			VideoPath:   "/data/work/test/" + shot.GetShotId() + ".mp4",
			DurationSec: 4,
			Engine:      "stock",
			Attempt:     req.GetAttempt(),
		},
		Feedback: &pb.CriticFeedback{Passed: true, Score: 0.9, Attempt: req.GetAttempt()},
	}, nil
}

// ---------------------------------------------------------------------------
// 测试夹具
// ---------------------------------------------------------------------------

type reviseHarness struct {
	proc   *Processor
	store  *store.Store
	queue  *queue.Client
	ai     *fakeAIServer
	jobID  string
	shotID string
}

// requireRedis 返回可用的 Redis 地址；不可达时跳过而不是失败。
//
// 跳过而非失败，是为了让没有 Redis 的机器上 `go test` 仍然是绿的；
// 同时在有 Redis 的机器上真的跑到。代价是「绿了但没跑」——
// 所以跳过原因里必须写清楚**跳过了什么**，让人一眼看出覆盖缺口。
func requireRedis(t *testing.T) string {
	t.Helper()
	addr := os.Getenv("SCID_TEST_REDIS_ADDR")
	if addr == "" {
		addr = "127.0.0.1:6379"
	}
	conn, err := net.DialTimeout("tcp", addr, 500*time.Millisecond)
	if err != nil {
		t.Skipf("Redis 不可达（%s），跳过 B4 幂等集成测试: %v", addr, err)
	}
	_ = conn.Close()
	return addr
}

func newReviseHarness(t *testing.T, fake *fakeAIServer) *reviseHarness {
	t.Helper()

	addr := requireRedis(t)
	ctx := context.Background()

	// 每个用例从干净的库开始：断言「队列里到底有几条」这类事实时，
	// 残留数据会让结果取决于执行顺序。
	rdb := goredis.NewClient(&goredis.Options{Addr: addr, DB: testRedisDB})
	if err := rdb.FlushDB(ctx).Err(); err != nil {
		t.Fatalf("清空测试库失败: %v", err)
	}
	_ = rdb.Close()

	redisCfg := config.RedisConfig{Addr: addr, DB: testRedisDB}

	st, err := store.New(ctx, redisCfg)
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

	cfg := &config.Config{
		Env:      "test",
		LogLevel: "error",
		Redis:    redisCfg,
		Queue: config.QueueConfig{
			Concurrency:  1,
			Queues:       map[string]int{queue.QueueCritical: 1, queue.QueueDefault: 1},
			MaxRetry:     3,
			RetryBackoff: time.Second,
			TaskTimeout:  time.Minute,
		},
		AI: config.AIConfig{
			Addr:             lis.Addr().String(),
			UnaryTimeout:     30 * time.Second,
			MaxRecvMsgSizeMB: 8,
		},
		Media: config.MediaConfig{
			FFmpegBin: "ffmpeg", FFprobeBin: "ffprobe",
			MaxParallel: 2, CommandTimeout: time.Minute, WorkDir: t.TempDir(),
		},
		Pipeline: config.PipelineConfig{ShotMaxAttempts: 3, CriticScoreThreshold: 0.75},
		Archive:  config.ArchiveConfig{Backend: "none"},
	}

	aiClient, err := ai.NewClient(cfg.AI, logger)
	if err != nil {
		t.Fatalf("构造 ai 客户端失败: %v", err)
	}
	t.Cleanup(func() { _ = aiClient.Close() })

	q := queue.NewClient(redisCfg, cfg.Queue)
	t.Cleanup(func() { _ = q.Close() })

	runner, err := media.NewRunner(cfg.Media)
	if err != nil {
		t.Fatalf("构造 media Runner 失败: %v", err)
	}

	// 归档后端取 none：缺省配置下不存在对象存储是**正常路径**，不该让测试绕开它。
	archiver, err := archive.New(archive.Options{Backend: "none"}, logger)
	if err != nil {
		t.Fatalf("构造归档器失败: %v", err)
	}

	h := &reviseHarness{
		proc:  NewProcessor(cfg, st, aiClient, q, runner, archiver, logger),
		store: st,
		queue: q,
		ai:    fake,
	}

	// job_id 带纳秒后缀：即使上一次运行的库没被清干净，也不会撞上旧任务的状态。
	h.jobID = "job-idem-" + strconv.FormatInt(time.Now().UnixNano(), 36)
	h.shotID = domain.ShotID(h.jobID, 0)
	h.seedShot(t)
	return h
}

// seedShot 写入一个处于 AWAITING_HUMAN 的单镜头任务，模拟「审核员打回后重新投递」的现场。
func (h *reviseHarness) seedShot(t *testing.T) {
	t.Helper()
	job := &domain.Job{
		JobID:             h.jobID,
		RawScript:         "测试脚本内容需要足够长以通过校验，这里补充一些文字凑够长度。",
		TargetDurationSec: 60,
		Locale:            "zh-CN",
		Status:            domain.JobRendering,
		CreatedAt:         time.Now().UTC(),
		Shots: []*domain.Shot{{
			ShotID:      h.shotID,
			JobID:       h.jobID,
			Index:       0,
			Tag:         domain.TagAmbience,
			Engine:      domain.EngineStock,
			DurationSec: 4,
			Narration:   "原始画外音",
			VisualBrief: "原始画面说明",
			Status:      domain.StatusAwaitingHuman,
			Attempt:     1,
		}},
	}
	if err := h.store.SaveJob(context.Background(), job); err != nil {
		t.Fatalf("写入测试任务失败: %v", err)
	}
}

// deliver 投递一次单镜头重做任务（等价于 Asynq 把同一条消息再送一次）。
func (h *reviseHarness) deliver(ctx context.Context, attempt int) error {
	return h.proc.HandleRenderShot(ctx, RenderShotTask{
		Task: asynq.NewTask(queue.TaskRenderShot, nil),
		Payload: &queue.RenderShotPayload{
			JobID:        h.jobID,
			ShotID:       h.shotID,
			Attempt:      attempt,
			HumanComment: "坐标轴标签重叠了，把字号调小并旋转 45 度",
			TriggeredBy:  "api",
		},
	})
}

func (h *reviseHarness) shot(t *testing.T) *domain.Shot {
	t.Helper()
	job, err := h.store.GetJob(context.Background(), h.jobID)
	if err != nil {
		t.Fatalf("读取任务失败: %v", err)
	}
	s := job.FindShot(h.shotID)
	if s == nil {
		t.Fatal("镜头消失了")
	}
	return s
}

// ---------------------------------------------------------------------------
// B4 用例
// ---------------------------------------------------------------------------

// TestB4ConcurrentDuplicateDeliveryRendersOnce 覆盖最危险的一种重复投递：
// 第一次**仍在渲染中**时，同一次 attempt 的消息又到达了一次。
//
// 用 fake 的 `entered` 通道做同步，而不是 sleep 赌时序 ——
// 赌时序的并发测试在 CI 上会变成随机红，最后被人加 skip 掉。
func TestB4ConcurrentDuplicateDeliveryRendersOnce(t *testing.T) {
	fake := &fakeAIServer{holdFor: 400 * time.Millisecond, entered: make(chan struct{})}
	h := newReviseHarness(t, fake)
	ctx := context.Background()

	firstDone := make(chan error, 1)
	go func() { firstDone <- h.deliver(ctx, 2) }()

	select {
	case <-fake.entered:
	case <-time.After(10 * time.Second):
		t.Fatal("第一次投递始终没有进入渲染，测试无法继续")
	}

	// 关键断言：此刻第一次渲染仍在进行，第二次投递必须被闸门挡下。
	if err := h.deliver(ctx, 2); err != nil {
		t.Fatalf("重复投递应当被安静地忽略，却返回了错误: %v", err)
	}
	if got := fake.reviseCalls.Load(); got != 1 {
		t.Errorf("重复投递并发到达时渲染了 %d 次，期望 1 次 —— 幂等闸门没起作用", got)
	}

	if err := <-firstDone; err != nil {
		t.Fatalf("第一次投递失败: %v", err)
	}

	// 完成之后再来一次：占位靠 30 分钟 TTL 兜住，仍不该重复渲染。
	if err := h.deliver(ctx, 2); err != nil {
		t.Fatalf("完成后的重复投递应当被忽略: %v", err)
	}
	if got := fake.reviseCalls.Load(); got != 1 {
		t.Errorf("完成后重复投递又渲染了一次（共 %d 次）—— 幂等占位提前失效了", got)
	}

	if s := h.shot(t); s.Status != domain.StatusApproved {
		t.Errorf("镜头状态期望 APPROVED，实际 %q", s.Status)
	}
}

// TestB4DifferentAttemptRendersAgain 是上一条的**反向对照**。
//
// 没有它，「永远返回『已在处理中』」这种退化实现也能让重复投递测试通过 ——
// 而那等于人工打回彻底失效（第二次打回永远不生效）。
func TestB4DifferentAttemptRendersAgain(t *testing.T) {
	fake := &fakeAIServer{}
	h := newReviseHarness(t, fake)
	ctx := context.Background()

	if err := h.deliver(ctx, 2); err != nil {
		t.Fatalf("attempt=2 投递失败: %v", err)
	}
	if err := h.deliver(ctx, 3); err != nil {
		t.Fatalf("attempt=3 投递失败: %v", err)
	}

	if got := fake.reviseCalls.Load(); got != 2 {
		t.Errorf("两次不同 attempt 应当各渲染一次（共 2 次），实际 %d 次", got)
	}
}

// TestB4IdempotencyKeyReleasedOnFailure 验证「真正失败必须释放占位」。
//
// 只占位不释放的后果很隐蔽：一次瞬时故障（Redis 抖动、AI 超时）会让该 attempt
// 在 TTL 内永久失去重试机会，任务卡在 GENERATING 而队列里空空如也。
func TestB4IdempotencyKeyReleasedOnFailure(t *testing.T) {
	fake := &fakeAIServer{failFirst: 1, failCode: codes.Internal}
	h := newReviseHarness(t, fake)
	ctx := context.Background()

	if err := h.deliver(ctx, 2); err == nil {
		t.Fatal("AI 返回 INTERNAL 时投递应当返回错误")
	}
	if got := fake.reviseCalls.Load(); got != 1 {
		t.Fatalf("首次投递应当调用一次渲染，实际 %d 次", got)
	}

	// 同一 attempt 再次投递（Asynq 重试）必须能真正重跑，而不是被自己的占位挡住。
	if err := h.deliver(ctx, 2); err != nil {
		t.Fatalf("失败后的重试不应被幂等占位挡住: %v", err)
	}
	if got := fake.reviseCalls.Load(); got != 2 {
		t.Errorf("失败后重试没有重新渲染（共 %d 次）—— 占位未释放，该 attempt 被永久锁死", got)
	}
}

// TestB4UnimplementedReleasesKeyAndSkipsRetry 验证灰度期的语义：
// 能力未实现要判为**不可重试**（不烧 attempt 计数），但占位同样要释放 ——
// 否则 Python 侧补上实现之后，同一次打回仍然永远跑不起来。
func TestB4UnimplementedReleasesKeyAndSkipsRetry(t *testing.T) {
	fake := &fakeAIServer{failFirst: 1, failCode: codes.Unimplemented}
	h := newReviseHarness(t, fake)
	ctx := context.Background()

	err := h.deliver(ctx, 2)
	if err == nil {
		t.Fatal("UNIMPLEMENTED 应当返回错误")
	}
	if !errors.Is(err, asynq.SkipRetry) {
		t.Errorf("UNIMPLEMENTED 必须包上 asynq.SkipRetry（否则会被反复重投），实际: %v", err)
	}
	if !errors.Is(err, ai.ErrNotImplemented) {
		t.Errorf("应当保留 ai.ErrNotImplemented 哨兵以便上层分类，实际: %v", err)
	}

	if err := h.deliver(ctx, 2); err != nil {
		t.Fatalf("能力未实现后再次投递不应被幂等占位挡住: %v", err)
	}
	if got := fake.reviseCalls.Load(); got != 2 {
		t.Errorf("占位未释放：Python 补上实现后同一次打回仍跑不起来（共 %d 次）", got)
	}
}
