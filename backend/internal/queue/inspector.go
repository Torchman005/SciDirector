package queue

import (
	"context"
	"fmt"
	"sort"
	"time"

	"github.com/hibiken/asynq"

	"github.com/itJinYu/SciDirector/backend/internal/config"
)

// QueueStat 是单个队列在某一时刻的深度快照。
//
// 字段名与 Asynq 的状态名一一对应，便于与 asynqmon 之类的工具对账。
type QueueStat struct {
	Queue string `json:"queue"`
	// Size 是**积压量**：等待被消费的任务数（pending + active + scheduled + retry）。
	// 这是判断"系统是否跟不上"最直接的指标。
	Size int `json:"size"`
	// Pending 是排队中（尚未被任何 worker 取走）的任务数。
	Pending int `json:"pending"`
	// Active 是正在执行的任务数。
	Active int `json:"active"`
	// Scheduled 是延后执行（含重试退避）的任务数。
	Scheduled int `json:"scheduled"`
	// Retry 是等待重试的任务数。持续不为 0 说明有任务在反复失败。
	Retry int `json:"retry"`
	// Archived 是已归档（不再重试）的任务数 —— 任务级最终失败都在这里。
	Archived int `json:"archived"`
	// Completed 是已完成数（Asynq 只保留一段时间内的记录，不是累计量）。
	Completed int `json:"completed"`
	// Paused 表示该队列被暂停（任务会堆积但不会被消费）。
	Paused bool `json:"paused"`
	// LatencySec 是最老待处理任务的等待时长，反映"用户要等多久才开始"。
	// 它比深度更能说明体感：深度 3 但都等了 10 分钟，与深度 300 但只等 1 秒，
	// 是完全不同的两种问题。
	LatencySec float64 `json:"latency_sec"`
}

// Stats 是整个队列系统的快照。
type Stats struct {
	Queues []QueueStat `json:"queues"`
	// Totals 是所有队列的汇总，便于告警规则直接取用。
	Totals QueueStat `json:"totals"`
	// FetchedAtUnixMs 让调用方能判断快照的新鲜度。
	FetchedAtUnixMs int64 `json:"fetched_at_unix_ms"`
}

// Inspector 基于 Asynq 的 Inspector 暴露队列深度与失败情况。
//
// 为什么这件事必须做：没有它，「任务卡住了」只能靠人工去 Redis 里翻 key；
// 而 Asynq 的队列状态本来就带 TTL，翻不到就等于没有证据。
type Inspector struct {
	insp   *asynq.Inspector
	queues []string
}

// NewInspector 构造队列观测器。
func NewInspector(cfg config.RedisConfig, qcfg config.QueueConfig) *Inspector {
	opt := asynq.RedisClientOpt{
		Addr:     cfg.Addr,
		Password: cfg.Password,
		DB:       cfg.DB,
	}

	names := make([]string, 0, len(qcfg.Queues))
	for name, weight := range qcfg.Queues {
		if weight <= 0 {
			continue
		}
		names = append(names, name)
	}
	if len(names) == 0 {
		names = append(names, QueueDefault)
	}
	// 排序保证输出稳定：map 遍历顺序随机，会让仪表盘上的队列顺序每次都在跳。
	sort.Strings(names)

	return &Inspector{insp: asynq.NewInspector(opt), queues: names}
}

// Queues 返回被观测的队列名。
func (i *Inspector) Queues() []string { return append([]string(nil), i.queues...) }

// Close 释放底层连接。
func (i *Inspector) Close() error { return i.insp.Close() }

// Stats 采集所有队列的快照。
//
// 先列出**现存队列**，再逐个取详情：不存在的队列直接按全 0 处理。
//
// 为什么需要这一步：Asynq 只在队列第一次收到任务时才创建它的元数据，
// 因此全新部署上所有队列都"不存在"。而 `GetQueueInfo` 在这种情况下返回的是
// 内部 RDB 的错误（`NOT_FOUND: queue "x" does not exist`），**既不包装
// `asynq.ErrQueueNotFound`，也没有任何可用的判定接口**（Asynq 自己的
// 文档说会 wrap，实际实现没有）。若把它当错误，统计接口会在最需要它的时刻
// ——确认"没东西卡住"——整个失败。
//
// 用 `Queues()` 列现存队列是文档化的做法，顺带还少一轮往返。
//
// 单个队列查询失败不会让整体失败：观测接口在 Redis 抖动时返回部分数据，
// 比整个接口 500 更有用 —— 部分数据至少能说明"挂在哪个队列上"。
func (i *Inspector) Stats(ctx context.Context) (Stats, error) {
	out := Stats{FetchedAtUnixMs: time.Now().UnixMilli()}

	existing, err := i.insp.Queues()
	if err != nil {
		return out, fmt.Errorf("queue: 列出队列失败: %w", err)
	}
	known := make(map[string]bool, len(existing))
	for _, name := range existing {
		known[name] = true
	}

	var firstErr error
	for _, name := range i.queues {
		if !known[name] {
			// 队列尚未创建 == 队列为空。对运维而言这两件事没有区别。
			stat := QueueStat{Queue: name}
			out.Queues = append(out.Queues, stat)
			mergeTotals(&out.Totals, stat)
			continue
		}

		stat, err := i.queueStat(ctx, name)
		if err != nil {
			if firstErr == nil {
				firstErr = err
			}
			continue
		}
		out.Queues = append(out.Queues, stat)
		mergeTotals(&out.Totals, stat)
	}
	out.Totals.Queue = "total"

	if len(out.Queues) == 0 && firstErr != nil {
		return out, firstErr
	}
	return out, nil
}

// queueStat 采集单个**已存在**队列的详情。
func (i *Inspector) queueStat(ctx context.Context, name string) (QueueStat, error) {
	if err := ctx.Err(); err != nil {
		return QueueStat{}, err
	}
	info, err := i.insp.GetQueueInfo(name)
	if err != nil {
		return QueueStat{}, fmt.Errorf("queue: 查询队列 %s 失败: %w", name, err)
	}
	return QueueStat{
		Queue:      name,
		Size:       info.Size,
		Pending:    info.Pending,
		Active:     info.Active,
		Scheduled:  info.Scheduled,
		Retry:      info.Retry,
		Archived:   info.Archived,
		Completed:  info.Completed,
		Paused:     info.Paused,
		LatencySec: info.Latency.Seconds(),
	}, nil
}

// mergeTotals 把单队列数据累加到汇总里。
//
// 延迟取**最大值**而不是求和：总延迟"加起来"没有意义，
// 用户关心的是最慢的那个队列要等多久。
func mergeTotals(total *QueueStat, s QueueStat) {
	total.Size += s.Size
	total.Pending += s.Pending
	total.Active += s.Active
	total.Scheduled += s.Scheduled
	total.Retry += s.Retry
	total.Archived += s.Archived
	total.Completed += s.Completed
	total.Paused = total.Paused || s.Paused
	if s.LatencySec > total.LatencySec {
		total.LatencySec = s.LatencySec
	}
}
