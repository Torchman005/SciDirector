package queue

import (
	"context"
	"net"
	"testing"
	"time"

	"github.com/hibiken/asynq"

	"github.com/itJinYu/SciDirector/backend/internal/config"
)

// testRedisAddr 返回可用的 Redis 地址；不可用时跳过测试。
//
// 跳过而不是失败：队列观测的代码路径不应该让「没起 Redis」的 CI 变红，
// 但本地（Redis 在跑）必须真的执行到 —— 所以这里做真实连接探测。
func testRedisAddr(t *testing.T) string {
	t.Helper()
	addr := "127.0.0.1:6379"
	conn, err := net.DialTimeout("tcp", addr, 500*time.Millisecond)
	if err != nil {
		t.Skipf("Redis 不可达（%s），跳过队列观测集成测试: %v", addr, err)
	}
	_ = conn.Close()
	return addr
}

func testInspector(t *testing.T) *Inspector {
	t.Helper()
	addr := testRedisAddr(t)
	// 用独立的 DB，避免与开发中的实例互相污染。
	return NewInspector(
		config.RedisConfig{Addr: addr, DB: 15},
		config.QueueConfig{Queues: map[string]int{"critical": 6, "default": 3, "ignored": 0}},
	)
}

// TestInspectorQueuesSkipsZeroWeight 验证权重为 0 的队列不被观测。
//
// 权重 0 在 Asynq 里表示「不消费该队列」，但那样队列仍然存在。
// 观测它只会让仪表盘上多一条永远是 0 的线，掩盖真正的问题。
func TestInspectorQueuesSkipsZeroWeight(t *testing.T) {
	addr := testRedisAddr(t)
	insp := NewInspector(
		config.RedisConfig{Addr: addr, DB: 15},
		config.QueueConfig{Queues: map[string]int{"critical": 6, "default": 3, "ignored": 0}},
	)
	defer func() { _ = insp.Close() }()

	got := insp.Queues()
	want := []string{"critical", "default"} // 已排序
	if len(got) != len(want) {
		t.Fatalf("Queues() = %v，期望 %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("Queues() = %v，期望 %v（且必须有序，否则仪表盘顺序会乱跳）", got, want)
		}
	}
}

// TestInspectorQueuesDefaultsWhenEmpty 覆盖未配置队列时的兜底。
func TestInspectorQueuesDefaultsWhenEmpty(t *testing.T) {
	addr := testRedisAddr(t)
	insp := NewInspector(config.RedisConfig{Addr: addr, DB: 15}, config.QueueConfig{})
	defer func() { _ = insp.Close() }()

	got := insp.Queues()
	if len(got) != 1 || got[0] != QueueDefault {
		t.Fatalf("未配置队列时应回落到 %s，实际 %v", QueueDefault, got)
	}
}

// TestInspectorStatsEnqueuedTaskIsVisible 是最有价值的一条：
// 真的往队列里塞一个任务，再断言观测接口**看得见它**。
//
// 只断言"接口返回 200 且字段齐全"是不够的 —— 那些字段全为 0 也能通过，
// 而"看不见队里的任务"正是这个功能要解决的核心问题。
func TestInspectorStatsEnqueuedTaskIsVisible(t *testing.T) {
	addr := testRedisAddr(t)
	insp := NewInspector(
		config.RedisConfig{Addr: addr, DB: 15},
		config.QueueConfig{Queues: map[string]int{"default": 1}},
	)
	defer func() { _ = insp.Close() }()

	ctx := context.Background()

	// 先清空，避免历史遗留任务影响断言。
	_, _ = insp.insp.DeleteAllPendingTasks("default")
	_, _ = insp.insp.DeleteAllScheduledTasks("default")
	_, _ = insp.insp.DeleteAllRetryTasks("default")
	_, _ = insp.insp.DeleteAllArchivedTasks("default")

	before, err := insp.Stats(ctx)
	if err != nil {
		t.Fatalf("采集失败: %v", err)
	}
	if before.Totals.Size != 0 {
		t.Fatalf("清空后积压应为 0，实际 %d", before.Totals.Size)
	}

	// 塞两个任务：一个立即执行、一个延后 1 小时（用于验证 scheduled 计数）。
	client := asynq.NewClient(asynq.RedisClientOpt{Addr: addr, DB: 15})
	defer func() { _ = client.Close() }()

	if _, err := client.Enqueue(asynq.NewTask(TaskComposeJob, []byte(`{"job_id":"obs-1"}`))); err != nil {
		t.Fatalf("入队失败: %v", err)
	}
	if _, err := client.Enqueue(asynq.NewTask(TaskComposeJob, []byte(`{"job_id":"obs-2"}`)),
		asynq.ProcessIn(time.Hour)); err != nil {
		t.Fatalf("延后入队失败: %v", err)
	}

	after, err := insp.Stats(ctx)
	if err != nil {
		t.Fatalf("采集失败: %v", err)
	}

	// 核心断言：入队的任务必须被看见。
	if after.Totals.Pending != 1 {
		t.Errorf("pending = %d，期望 1（立即执行的那个）", after.Totals.Pending)
	}
	if after.Totals.Scheduled != 1 {
		t.Errorf("scheduled = %d，期望 1（延后一小时的那个）", after.Totals.Scheduled)
	}
	// Size 是积压量，四个状态之和。
	if after.Totals.Size != 2 {
		t.Errorf("size = %d，期望 2", after.Totals.Size)
	}
	// 单个队列的明细也要对得上。
	if len(after.Queues) != 1 {
		t.Fatalf("应返回 1 个队列，实际 %d", len(after.Queues))
	}
	if after.Queues[0].Queue != "default" {
		t.Errorf("队列名 = %q，期望 default", after.Queues[0].Queue)
	}
	if after.FetchedAtUnixMs == 0 {
		t.Error("快照时间戳未填充，调用方无法判断新鲜度")
	}

	// 收尾，避免给后续运行留垃圾。
	_, _ = insp.insp.DeleteAllPendingTasks("default")
	_, _ = insp.insp.DeleteAllScheduledTasks("default")
}

// TestInspectorStatsOnMissingQueueIsZeroNotError 覆盖「队列从未有任务」。
//
// Asynq 只在队列第一次收到任务时才创建元数据，因此全新部署上所有队列都
// "不存在"。此时必须当作**空队列**返回全 0，而不是报错 ——
// 统计接口若在最需要它的时刻（确认"没东西卡住"）整个失败，那它就白做了。
//
// 对运维而言「队列从未有任务」与「队列是空的」本来就是同一件事。
func TestInspectorStatsOnMissingQueueIsZeroNotError(t *testing.T) {
	addr := testRedisAddr(t)
	insp := NewInspector(
		config.RedisConfig{Addr: addr, DB: 15},
		config.QueueConfig{Queues: map[string]int{"never-created-queue": 1}},
	)
	defer func() { _ = insp.Close() }()

	stats, err := insp.Stats(context.Background())
	if err != nil {
		t.Fatalf("队列不存在不应报错，实际: %v", err)
	}
	if len(stats.Queues) != 1 {
		t.Fatalf("应返回 1 个队列的快照，实际 %d", len(stats.Queues))
	}
	if stats.Queues[0].Queue != "never-created-queue" {
		t.Errorf("队列名 = %q", stats.Queues[0].Queue)
	}
	if stats.Totals.Size != 0 || stats.Totals.Pending != 0 || stats.Totals.Archived != 0 {
		t.Errorf("不存在的队列应报告全 0，实际 %+v", stats.Totals)
	}
}

// TestMergeTotalsLatencyTakesMax 覆盖汇总语义：
// 延迟取最大值而不是求和 —— "总延迟加起来"没有意义。
func TestMergeTotalsLatencyTakesMax(t *testing.T) {
	total := QueueStat{}
	mergeTotals(&total, QueueStat{Queue: "a", Size: 1, Pending: 1, LatencySec: 3.5})
	mergeTotals(&total, QueueStat{Queue: "b", Size: 2, Pending: 2, Retry: 1, LatencySec: 1.0})

	if total.Size != 3 || total.Pending != 3 || total.Retry != 1 {
		t.Errorf("计数应累加，实际 size=%d pending=%d retry=%d",
			total.Size, total.Pending, total.Retry)
	}
	if total.LatencySec != 3.5 {
		t.Errorf("延迟应取最大值 3.5，实际 %.2f", total.LatencySec)
	}
}

// TestMergeTotalsPausedIsSticky 覆盖暂停标记：
// 任一个队列被暂停都应体现在汇总上，否则告警会漏。
func TestMergeTotalsPausedIsSticky(t *testing.T) {
	total := QueueStat{}
	mergeTotals(&total, QueueStat{Queue: "a", Paused: false})
	mergeTotals(&total, QueueStat{Queue: "b", Paused: true})
	if !total.Paused {
		t.Fatal("任一队列暂停时汇总应为暂停")
	}
}
