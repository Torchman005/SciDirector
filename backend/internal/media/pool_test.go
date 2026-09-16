package media

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"
	"time"
)

// concurrencyTracker 记录「同时在飞的调用数」与历史峰值。
//
// 用 atomic 而不是普通变量：这些方法会被多个 goroutine 同时调用，
// 测试代码自身有数据竞争的话，跑 -race 时会掩盖真正要测的问题。
type concurrencyTracker struct {
	inFlight atomic.Int64
	peak     atomic.Int64
	total    atomic.Int64
}

func (c *concurrencyTracker) enter() {
	c.total.Add(1)
	n := c.inFlight.Add(1)
	for {
		old := c.peak.Load()
		if n <= old || c.peak.CompareAndSwap(old, n) {
			return
		}
	}
}

func (c *concurrencyTracker) leave() { c.inFlight.Add(-1) }

// TestPoolLimitsConcurrency 是本次改动的**核心断言**：
// 无界 fan-out 会为每个元素起一个 goroutine，Pool 必须把同时在飞的数量压在上限内。
func TestPoolLimitsConcurrency(t *testing.T) {
	const limit = 3
	const items = 20

	pool := NewPool(limit)
	var tr concurrencyTracker

	err := pool.Run(context.Background(), items, func(ctx context.Context, i int) error {
		tr.enter()
		defer tr.leave()
		// 睡够久，确保前 limit 个分支一定重叠。否则调度器可能让它们错开，
		// 「峰值 <= limit」会变成一条永远成立的空断言。
		time.Sleep(20 * time.Millisecond)
		return nil
	})
	if err != nil {
		t.Fatalf("Run 返回意外错误: %v", err)
	}

	if got := tr.total.Load(); got != items {
		t.Fatalf("应执行 %d 个分支，实际 %d", items, got)
	}
	if peak := tr.peak.Load(); peak > limit {
		t.Fatalf("并发峰值 %d 超过上限 %d —— Worker Pool 没有生效", peak, limit)
	}
	// 反向断言同样重要：如果峰值只有 1，说明退化成串行了，
	// 「限制了并发」和「根本没并发」必须区分开。
	if peak := tr.peak.Load(); peak != limit {
		t.Fatalf("并发峰值 %d，期望恰好达到上限 %d（并发未真正发生）", peak, limit)
	}
}

// TestPoolLimitNeverBelowOne 守住「不限流」这个危险默认值不被引入。
func TestPoolLimitNeverBelowOne(t *testing.T) {
	for _, in := range []int{0, -1, -100} {
		if got := NewPool(in).Limit(); got != 1 {
			t.Fatalf("NewPool(%d).Limit() = %d，期望 1", in, got)
		}
	}
	if got := NewPool(7).Limit(); got != 7 {
		t.Fatalf("NewPool(7).Limit() = %d，期望 7", got)
	}
}

// TestPoolLimitAboveItemCountIsHarmless 覆盖「上限大于元素数」的边界，
// 此时实际并发度应被元素数封顶而不是报错。
func TestPoolLimitAboveItemCountIsHarmless(t *testing.T) {
	pool := NewPool(100)
	var tr concurrencyTracker
	err := pool.Run(context.Background(), 2, func(ctx context.Context, i int) error {
		tr.enter()
		defer tr.leave()
		time.Sleep(10 * time.Millisecond)
		return nil
	})
	if err != nil {
		t.Fatalf("Run 返回意外错误: %v", err)
	}
	if peak := tr.peak.Load(); peak > 2 {
		t.Fatalf("并发峰值 %d 超过元素数 2", peak)
	}
}

// TestPoolZeroItems 覆盖空输入：不应返回错误，也不应调用 fn。
func TestPoolZeroItems(t *testing.T) {
	pool := NewPool(4)
	called := atomic.Int64{}
	if err := pool.Run(context.Background(), 0, func(context.Context, int) error {
		called.Add(1)
		return nil
	}); err != nil {
		t.Fatalf("空输入不应返回错误，实际: %v", err)
	}
	if called.Load() != 0 {
		t.Fatalf("空输入不应调用 fn，实际调用 %d 次", called.Load())
	}
}

// TestPoolRejectsCancelledContext 保证已经取消的 ctx 不会静默地照常干活。
func TestPoolRejectsCancelledContext(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	pool := NewPool(2)
	called := atomic.Int64{}
	err := pool.Run(ctx, 5, func(context.Context, int) error {
		called.Add(1)
		return nil
	})
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("期望 context.Canceled，实际: %v", err)
	}
	if called.Load() != 0 {
		t.Fatalf("ctx 已取消却仍执行了 %d 个分支", called.Load())
	}
}

// TestPoolFailFastCancelsSiblings 验证「一个分支失败 → 其余分支被取消」。
func TestPoolFailFastCancelsSiblings(t *testing.T) {
	boom := errors.New("boom")
	pool := NewPool(3)
	var cancelled atomic.Int64
	var tr concurrencyTracker

	err := pool.Run(context.Background(), 30, func(ctx context.Context, i int) error {
		tr.enter()
		defer tr.leave()
		if i == 0 {
			return boom
		}
		// 其余分支等待取消信号。若不能快速失败，它们会睡满 2 秒。
		select {
		case <-ctx.Done():
			cancelled.Add(1)
			return ctx.Err()
		case <-time.After(2 * time.Second):
			return nil
		}
	})

	if !errors.Is(err, boom) {
		t.Fatalf("期望返回根因 %v，实际: %v", boom, err)
	}
	if cancelled.Load() == 0 {
		t.Fatal("没有任何兄弟分支收到取消信号 —— 快速失败未生效")
	}
	// 返回时不允许还有分支在飞，否则调用方接着写共享状态就会产生数据竞争。
	if n := tr.inFlight.Load(); n != 0 {
		t.Fatalf("Run 返回时仍有 %d 个分支在飞 —— 存在 goroutine 泄漏", n)
	}
}

// TestPoolRunWaitsForAllBranches 是并发代码最容易踩的坑：
// 「Run 返回了，但后台还有 goroutine 在写我的切片」。
// 这里用 -race 跑时，任何未收敛的分支都会让断言或 race detector 报错。
func TestPoolRunWaitsForAllBranches(t *testing.T) {
	const items = 12
	pool := NewPool(4)

	results := make([]int, items)
	err := pool.Run(context.Background(), items, func(ctx context.Context, i int) error {
		// 故意让不同分支耗时差异很大，制造「先完成的先返回」的错觉。
		time.Sleep(time.Duration(items-i) * 3 * time.Millisecond)
		results[i] = i * i
		return nil
	})
	if err != nil {
		t.Fatalf("Run 返回意外错误: %v", err)
	}
	for i, got := range results {
		if got != i*i {
			t.Fatalf("results[%d] = %d，期望 %d（Run 未等待全部分支收敛）", i, got, i*i)
		}
	}
}

// TestMapWritesByIndex 验证泛型包装按**下标**写回而不是按完成顺序，
// 否则分镜顺序会被打乱 —— 那会直接毁掉叙事顺序。
func TestMapWritesByIndex(t *testing.T) {
	pool := NewPool(4)
	in := []int{10, 20, 30, 40, 50, 60}

	out, err := Map(context.Background(), pool, in, func(ctx context.Context, i int, v int) (string, error) {
		// 下游耗时与下标成反比：先完成的一定是后面的元素。
		time.Sleep(time.Duration(len(in)-i) * 2 * time.Millisecond)
		return string(rune('a' + i)), nil
	})
	if err != nil {
		t.Fatalf("Map 返回意外错误: %v", err)
	}
	for i := range in {
		want := string(rune('a' + i))
		if out[i] != want {
			t.Fatalf("out[%d] = %q，期望 %q（结果顺序被打乱）", i, out[i], want)
		}
	}
}

// TestMapPropagatesError 验证 Map 在分支失败时返回 nil 结果而不是半成品切片：
// 「部分结果」比明确的失败危险得多，调用方很容易把零值当成真实数据用下去。
func TestMapPropagatesError(t *testing.T) {
	boom := errors.New("bad shot")
	pool := NewPool(3)
	out, err := Map(context.Background(), pool, []int{1, 2, 3, 4}, func(ctx context.Context, i int, v int) (string, error) {
		if v == 3 {
			return "", boom
		}
		return "ok", nil
	})
	if !errors.Is(err, boom) {
		t.Fatalf("期望 %v，实际 %v", boom, err)
	}
	if out != nil {
		t.Fatalf("失败时应返回 nil 而非部分结果，实际长度 %d", len(out))
	}
}

// TestSemaphoreIsIdempotentOnRelease 守住双重释放：
// 释放函数被调用两次会让信号量容量凭空变大，并发上限静默失效。
func TestSemaphoreIsIdempotentOnRelease(t *testing.T) {
	s := NewSemaphore(1)
	release, err := s.Acquire(context.Background())
	if err != nil {
		t.Fatalf("Acquire 失败: %v", err)
	}
	release()
	release() // 第二次必须是 no-op

	// 容量应仍为 1：能拿到一次，但拿不到第二次。
	r1, err := s.Acquire(context.Background())
	if err != nil {
		t.Fatalf("释放后应能再次 Acquire: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	if _, err := s.Acquire(ctx); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("槽位已满时应阻塞至超时，实际: %v", err)
	}
	r1()
}

// TestSemaphoreTimeoutDoesNotLeakSlot 验证「等待超时的 Acquire 不会偷走槽位」。
//
// 这是信号量实现最容易写错的地方：如果在 select 的 ctx.Done() 分支里
// 也执行了 `<-s.ch`，那么每一次超时都会**凭空吃掉一个槽位**，
// 容量逐渐归零，最终整条流水线彻底卡死且没有任何错误可报。
func TestSemaphoreTimeoutDoesNotLeakSlot(t *testing.T) {
	s := NewSemaphore(1)

	// 占满唯一的槽位。
	held, err := s.Acquire(context.Background())
	if err != nil {
		t.Fatalf("首次 Acquire 失败: %v", err)
	}

	// 这次一定拿不到，应阻塞至超时。
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	if _, err := s.Acquire(ctx); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("槽位已满时应返回 DeadlineExceeded，实际: %v", err)
	}

	// 关键断言：归还占用的槽位后，容量必须仍是 1。
	held()
	back, err := s.Acquire(context.Background())
	if err != nil {
		t.Fatalf("归还槽位后应能再次 Acquire（说明超时路径泄漏了槽位）: %v", err)
	}
	back()
}
