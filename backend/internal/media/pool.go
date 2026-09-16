package media

import (
	"context"

	"golang.org/x/sync/errgroup"
)

// Pool 是一个**有界并发执行器**（Worker Pool）。
//
// 为什么需要它，而不是直接 `for { go func() {} }`？
//
// 天真的写法会为每个元素起一个 goroutine，再用信号量限制「真正干活」的数量。
// 这能限制并发度，但**并不限制 goroutine 数量**：一个 500 分镜的视频会瞬间
// 产生 500 个 goroutine 阻塞在信号量上。goroutine 虽然廉价（初始栈约 8KB），
// 但它们持有的闭包、被闭包引用的分镜数据、以及每个 goroutine 自己的栈都会
// 随元素数量线性增长 —— 这正是「大任务把内存打爆」的典型路径。
//
// Pool 的语义是：**同时在飞的 goroutine 数**不超过 limit，生产者循环本身
// 会被阻塞。拿不到槽位时根本不会创建 goroutine，因此内存占用是 O(limit)
// 而不是 O(n)。
//
// 与 Runner 的全局进程闸门的关系：
//
//	Pool     —— 限制「一个任务内部」的并发度，防止 goroutine 爆炸；
//	Runner.sem —— 限制「整个进程」的 ffmpeg 子进程数，防止 CPU/内存被吃满。
//
// 两者是乘法关系而不是重复：多个任务各自受 Pool 约束，而它们加起来仍受
// Runner.sem 这个**全局**上限约束。真正的 OOM 防线是后者。
type Pool struct {
	limit int
}

// NewPool 创建一个并发上限为 limit 的 Worker Pool。
// limit < 1 时按 1 处理 —— 「不限流」是一个危险的默认值，绝不提供。
func NewPool(limit int) *Pool {
	if limit < 1 {
		limit = 1
	}
	return &Pool{limit: limit}
}

// Limit 返回该 Pool 的并发上限。
func (p *Pool) Limit() int { return p.limit }

// Run 并发地对 [0, n) 的每个下标调用 fn，并发度不超过 Pool 的上限。
//
// 错误与取消语义（这三条是并发代码最容易写错的地方）：
//
//  1. **快速失败**：任一分支返回错误，立即取消其余分支的派生 context。
//     正在跑的 ffmpeg 会收到取消并退出，不会白烧 CPU。
//
//  2. **等待收敛**：Run 一定会等**所有已启动的分支真正返回**后才返回。
//     绝不允许「返回了但后台还有 goroutine 在写调用方的切片」——
//     那是 Go 里最常见的数据竞争来源，且极难复现。
//
//  3. **返回首个错误**：errgroup 保证返回最先发生的那个错误。被取消的分支
//     返回的 context.Canceled 不会覆盖真正的根因。
//
// fn 收到的 ctx 是派生 context：它会在任一分支失败时被取消。fn 内的所有
// 阻塞操作（尤其是外部进程调用）都**必须**透传这个 ctx，否则第 1 条失效。
func (p *Pool) Run(ctx context.Context, n int, fn func(ctx context.Context, i int) error) error {
	if n <= 0 {
		return nil
	}
	if err := ctx.Err(); err != nil {
		return err
	}

	// limit 不需要超过元素个数。
	limit := p.limit
	if limit > n {
		limit = n
	}

	g, gctx := errgroup.WithContext(ctx)
	// SetLimit 让 g.Go 在槽位用尽时**阻塞**，于是这个循环本身就成了
	// 有界的生产者 —— 这正是「Worker Pool」与「无脑 fan-out + 信号量」的区别。
	g.SetLimit(limit)

	for i := 0; i < n; i++ {
		i := i
		g.Go(func() error {
			// 已经有分支失败了：不要再启动新的重活。
			// 注意这里返回 gctx.Err() 而不是 nil，语义上是「这一路被取消了」，
			// 但因为 errgroup 只保留第一个错误，它不会掩盖真正的根因。
			select {
			case <-gctx.Done():
				return gctx.Err()
			default:
			}
			return fn(gctx, i)
		})
	}
	return g.Wait()
}

// Map 是 Run 的便利包装：对 items 的每个元素并发求值，结果按下标写回。
//
// 适用于「每个分镜都要 probe / 归一化 / 抽帧」这类天然的下标映射场景。
func Map[T any, R any](ctx context.Context, p *Pool, items []T,
	fn func(ctx context.Context, i int, item T) (R, error),
) ([]R, error) {
	out := make([]R, len(items))
	// 每个下标只被自己的分支写入 —— 无共享写入，因此不需要加锁。
	err := p.Run(ctx, len(items), func(ctx context.Context, i int) error {
		v, err := fn(ctx, i, items[i])
		if err != nil {
			return err
		}
		out[i] = v
		return nil
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}
