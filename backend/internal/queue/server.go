package queue

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/hibiken/asynq"

	"github.com/itJinYu/SciDirector/backend/internal/config"
)

// NewServer 构造 Asynq 服务端。
//
// 关键参数说明：
//   - Concurrency：同时在跑的**任务**数。注意单个生成任务本身还会再并发调用
//     ffmpeg（受 SCID_FFMPEG_MAX_PARALLEL 限制），两者相乘才是真实资源占用，
//     调参时必须一起考虑，否则会把机器打爆。
//   - StrictPriority：critical 队列先于 default 清空，保证人工打回的重做任务
//     不会被新提交的大任务饿死（HITL 的体验直接取决于此）。
//   - ErrorHandler：把任务级错误统一进结构化日志，便于按 job_id 聚合告警。
func NewServer(cfg config.RedisConfig, qcfg config.QueueConfig, logger *slog.Logger) *asynq.Server {
	queues := make(map[string]int, len(qcfg.Queues))
	for name, weight := range qcfg.Queues {
		if weight <= 0 {
			continue
		}
		queues[name] = weight
	}
	// 兜底：配置写错导致没有任何有效队列时，必须给一个默认队列，
	// 否则服务「起来了但不消费任何任务」——这类静默故障最难排查。
	if len(queues) == 0 {
		queues[QueueDefault] = 1
	}

	srv := asynq.NewServer(redisOpt(cfg), asynq.Config{
		Concurrency:         qcfg.Concurrency,
		Queues:              queues,
		StrictPriority:      true,
		ShutdownTimeout:     30 * time.Second,
		HealthCheckInterval: 15 * time.Second,
		// RetryDelayFunc 在此作为兜底；入队侧若显式指定则以入队侧为准。
		RetryDelayFunc: func(n int, _ error, _ *asynq.Task) time.Duration {
			return backoff(qcfg.RetryBackoff, n)
		},
		ErrorHandler: asynq.ErrorHandlerFunc(func(ctx context.Context, task *asynq.Task, err error) {
			// 此处拿到的是 asynq 层错误（任务超时、重试耗尽等）；
			// 业务语义错误已在 handler 内部处理并记录，二者不会互相掩盖。
			retryCount, _ := asynq.GetRetryCount(ctx)
			maxRetry, _ := asynq.GetMaxRetry(ctx)
			logger.Error("asynq 任务失败",
				slog.String("task_type", task.Type()),
				slog.Int("retry_count", retryCount),
				slog.Int("max_retry", maxRetry),
				slog.String("error", err.Error()),
			)
		}),
		Logger: asynqLogger{logger: logger},
	})
	return srv
}

// NewMux 创建任务分发器。handler 在 worker 包中注册，避免 queue 包反向依赖业务逻辑。
func NewMux() *asynq.ServeMux { return asynq.NewServeMux() }

// backoff 计算指数退避时长，并设置上限与抖动。
//
// 抖动（jitter）是必要的：大量任务在同一时刻失败时，若退避时长完全一致，
// 它们会在同一秒集体重试，形成周期性惊群，把刚恢复的下游再次打垮。
func backoff(base time.Duration, n int) time.Duration {
	if base <= 0 {
		base = 30 * time.Second
	}
	if n < 0 {
		n = 0
	}
	if n > 6 { // 2^6 = 64 倍，再大就超过上限没有意义
		n = 6
	}
	d := base * time.Duration(1<<uint(n))
	if d > 30*time.Minute {
		d = 30 * time.Minute
	}
	// 抖动幅度控制在该次退避的 ±10%。
	jitter := time.Duration(int64(d) / 10)
	if jitter <= 0 {
		return d
	}
	// 使用纳秒时间作为伪随机源，避免引入 math/rand 的全局锁与种子问题。
	delta := time.Duration(time.Now().UnixNano()%int64(2*jitter)) - jitter
	return d + delta
}

// asynqLogger 把 asynq 内部日志桥接到 slog，保证全系统日志格式与字段一致。
type asynqLogger struct{ logger *slog.Logger }

func (l asynqLogger) Debug(args ...interface{}) { l.logger.Debug(fmt.Sprint(args...)) }
func (l asynqLogger) Info(args ...interface{})  { l.logger.Info(fmt.Sprint(args...)) }
func (l asynqLogger) Warn(args ...interface{})  { l.logger.Warn(fmt.Sprint(args...)) }
func (l asynqLogger) Error(args ...interface{}) { l.logger.Error(fmt.Sprint(args...)) }
func (l asynqLogger) Fatal(args ...interface{}) { l.logger.Error(fmt.Sprint(args...)) }
