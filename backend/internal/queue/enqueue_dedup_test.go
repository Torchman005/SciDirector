package queue

// 本文件落地**阶段三验收 B4 的入队侧**：同一 `(job, shot, attempt)` 只入队一份。
//
// 与执行侧那道闸门（`worker/idempotency_test.go`）防的不是同一件事：
// 这里挡的是「人工连点两下」「前端重试」这类**重复入队**，
// 它连任务都不会进队列；而执行侧挡的是 Asynq 自己「执行成功但确认失败」后的重复投递。
// 两道都需要：只有入队去重时，Asynq 的重复投递会真的把镜头渲两遍。
//
// `EnqueueRenderShot` 用的是 **`asynq.TaskID`** 而不是只靠 `asynq.Unique`：
// Unique 的键由 (队列, 任务类型, 载荷) 计算而来，载荷里带着 `human_comment` ——
// 同一个 attempt 若因不同意见被投两次，Unique 键不同、去重就失效了。
// TaskID 由 `job+shot+attempt` 直接拼出，才是真正表达「这是同一次尝试」的那个键。

import (
	"context"
	"testing"
	"time"

	goredis "github.com/redis/go-redis/v9"

	"github.com/itJinYu/SciDirector/backend/internal/config"
)

// testEnqueueDB 是本文件用例独占的库号：与观测用例的 15 错开。
// 这里会 FlushDB，独立库号能保证不会顺手清掉别的用例正在断言的键。
const testEnqueueDB = 12

func newEnqueueClient(t *testing.T) (*Client, config.RedisConfig) {
	t.Helper()
	addr := testRedisAddr(t)

	rdb := goredis.NewClient(&goredis.Options{Addr: addr, DB: testEnqueueDB})
	if err := rdb.FlushDB(context.Background()).Err(); err != nil {
		t.Fatalf("清空测试库失败: %v", err)
	}
	_ = rdb.Close()

	cfg := config.RedisConfig{Addr: addr, DB: testEnqueueDB}
	c := NewClient(cfg, config.QueueConfig{
		MaxRetry:     3,
		RetryBackoff: 30 * time.Second,
		TaskTimeout:  60 * time.Second,
	})
	t.Cleanup(func() { _ = c.Close() })
	return c, cfg
}

// pendingIn 返回指定队列的待处理任务数。
//
// 先 `Queues()` 再取值，而不是直接 `GetQueueInfo`：
// Asynq 对**不存在的队列**返回的是内部 RDB 错误（并不包装成 ErrQueueNotFound，
// 见 Agent.md §9），直接调用会让「还没有过任务」这种正常状态被误判为故障。
func pendingIn(t *testing.T, c *Client, cfg config.RedisConfig, queueName string) int {
	t.Helper()
	insp := c.Inspector(cfg)
	defer func() { _ = insp.Close() }()

	names, err := insp.Queues()
	if err != nil {
		t.Fatalf("列出队列失败: %v", err)
	}
	exists := false
	for _, q := range names {
		if q == queueName {
			exists = true
			break
		}
	}
	if !exists {
		return 0
	}
	info, err := insp.GetQueueInfo(queueName)
	if err != nil {
		t.Fatalf("读取队列 %s 信息失败: %v", queueName, err)
	}
	return info.Pending
}

func renderShotPayload(attempt int, comment string) *RenderShotPayload {
	return &RenderShotPayload{
		JobID:        "job-b4-enq",
		ShotID:       "job-b4-enq-s000",
		Attempt:      attempt,
		HumanComment: comment,
		TriggeredBy:  "api",
	}
}

// TestB4EnqueueRenderShotDeduplicatesSameAttempt 覆盖「人工连点两下」。
//
// 期望的第二个返回值是 `("", nil)` —— 重复投递对调用方而言是**幂等成功**，
// 不是错误。把它做成错误会让 HTTP 层返回 409，用户看到的是「打回失败」，
// 而实际上第一次已经生效了。
func TestB4EnqueueRenderShotDeduplicatesSameAttempt(t *testing.T) {
	c, cfg := newEnqueueClient(t)
	ctx := context.Background()

	if _, err := c.EnqueueRenderShot(ctx, renderShotPayload(2, "画面太挤")); err != nil {
		t.Fatalf("首次投递失败: %v", err)
	}
	if _, err := c.EnqueueRenderShot(ctx, renderShotPayload(2, "画面太挤")); err != nil {
		t.Fatalf("重复投递应当是幂等成功而不是报错: %v", err)
	}

	if got := pendingIn(t, c, cfg, QueueCritical); got != 1 {
		t.Errorf("重复投递后队列里有 %d 条任务，期望 1 条 —— 同一个 attempt 被排了两次，会渲染两遍", got)
	}

	// 换一条**不同的意见**、但仍是同一个 attempt：载荷变了，TaskID 没变。
	// 这是 TaskID 相比 Unique 更可靠的地方，必须一并钉住。
	if _, err := c.EnqueueRenderShot(ctx, renderShotPayload(2, "坐标轴标签重叠")); err != nil {
		t.Fatalf("同 attempt 但不同意见的重复投递应当被忽略: %v", err)
	}
	if got := pendingIn(t, c, cfg, QueueCritical); got != 1 {
		t.Errorf("载荷变化后去重失效（队列 %d 条）：只靠 Unique 会漏掉这种情况", got)
	}
}

// TestB4EnqueueRenderShotAllowsNewAttempt 是反向对照。
//
// 若把去重做成「同一镜头只允许一条任务」，人工打回就只可能生效一次 ——
// 第二次打回会被静默吞掉，审核员以为提交成功、画面却永远不变。
func TestB4EnqueueRenderShotAllowsNewAttempt(t *testing.T) {
	c, cfg := newEnqueueClient(t)
	ctx := context.Background()

	for _, attempt := range []int{2, 3} {
		if _, err := c.EnqueueRenderShot(ctx, renderShotPayload(attempt, "继续调整")); err != nil {
			t.Fatalf("attempt=%d 投递失败: %v", attempt, err)
		}
	}

	if got := pendingIn(t, c, cfg, QueueCritical); got != 2 {
		t.Errorf("两次不同 attempt 应当各入队一条（期望 2），实际 %d —— 人工打回只能生效一次", got)
	}
}
