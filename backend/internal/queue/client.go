package queue

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/hibiken/asynq"

	"github.com/itJinYu/SciDirector/backend/internal/config"
)

// Client 是任务入队门面，由 API 层使用。
type Client struct {
	cli *asynq.Client
	cfg config.QueueConfig
}

// NewClient 构造入队客户端。底层复用 Redis 连接配置。
func NewClient(cfg config.RedisConfig, qcfg config.QueueConfig) *Client {
	opt := redisOpt(cfg)
	return &Client{cli: asynq.NewClient(opt), cfg: qcfg}
}

// Close 释放连接。
func (c *Client) Close() error { return c.cli.Close() }

// redisOpt 把项目配置翻译为 asynq 的连接选项。
func redisOpt(cfg config.RedisConfig) asynq.RedisClientOpt {
	return asynq.RedisClientOpt{
		Addr:     cfg.Addr,
		Password: cfg.Password,
		DB:       cfg.DB,
	}
}

// defaultRetry 返回统一的入队选项。
//
// Retention 的取值理由是「可观测性优先」：
// 失败任务保留 7 天（asynq 默认），成功任务保留 1 天，方便事后回放排查。
//
// 注意：**重试退避（RetryDelayFunc）只能在 server 端配置**，
// asynq v0.24 没有提供任务级的退避选项，因此该项统一放在 queue.NewServer 中。
func (c *Client) defaultRetry(queue string) []asynq.Option {
	return []asynq.Option{
		asynq.Queue(queue),
		asynq.MaxRetry(c.cfg.MaxRetry),
		asynq.Timeout(c.cfg.TaskTimeout),
		asynq.Retention(24 * time.Hour),
	}
}

// EnqueueGenerateJob 投递一次完整的生成任务。
func (c *Client) EnqueueGenerateJob(ctx context.Context, p *GenerateJobPayload) (string, error) {
	if p.EnqueuedAt.IsZero() {
		p.EnqueuedAt = time.Now().UTC()
	}
	buf, err := Encode(p)
	if err != nil {
		return "", err
	}
	task := asynq.NewTask(TaskGenerateJob, buf)

	opts := c.defaultRetry(QueueCritical)
	// 同一 job 的生成任务天然唯一：即使前端重复点击，也只保留一份。
	opts = append(opts, asynq.Unique(30*time.Minute))

	info, err := c.cli.EnqueueContext(ctx, task, opts...)
	if err != nil {
		// asynq.ErrDuplicateTask 表示任务已在队列中，对调用方而言是幂等成功。
		if errors.Is(err, asynq.ErrDuplicateTask) {
			return "", nil
		}
		return "", fmt.Errorf("queue: 投递生成任务失败: %w", err)
	}
	return info.ID, nil
}

// EnqueueRenderShot 投递单镜头重做任务（人类反馈闭环入口）。
func (c *Client) EnqueueRenderShot(ctx context.Context, p *RenderShotPayload) (string, error) {
	if p.EnqueuedAt.IsZero() {
		p.EnqueuedAt = time.Now().UTC()
	}
	buf, err := Encode(p)
	if err != nil {
		return "", err
	}
	task := asynq.NewTask(TaskRenderShot, buf)

	opts := c.defaultRetry(QueueCritical)
	// 唯一性键按「job+shot+attempt」组合：
	// 允许同一镜头重做多次（人工可能连续打回），但同一次 attempt 不会被重复执行。
	opts = append(opts, asynq.Unique(10*time.Minute),
		asynq.TaskID(fmt.Sprintf("shot-%s-%s-%d", p.JobID, p.ShotID, p.Attempt)))

	info, err := c.cli.EnqueueContext(ctx, task, opts...)
	if err != nil {
		if errors.Is(err, asynq.ErrDuplicateTask) || errors.Is(err, asynq.ErrTaskIDConflict) {
			return "", nil
		}
		return "", fmt.Errorf("queue: 投递镜头重做任务失败: %w", err)
	}
	return info.ID, nil
}

// EnqueueComposeJob 投递合成任务（低优先级，避免抢占关键路径）。
func (c *Client) EnqueueComposeJob(ctx context.Context, p *ComposeJobPayload) (string, error) {
	if p.EnqueuedAt.IsZero() {
		p.EnqueuedAt = time.Now().UTC()
	}
	buf, err := Encode(p)
	if err != nil {
		return "", err
	}
	task := asynq.NewTask(TaskComposeJob, buf)
	opts := c.defaultRetry(QueueDefault)
	opts = append(opts, asynq.Unique(30*time.Minute))

	info, err := c.cli.EnqueueContext(ctx, task, opts...)
	if err != nil {
		if errors.Is(err, asynq.ErrDuplicateTask) {
			return "", nil
		}
		return "", fmt.Errorf("queue: 投递合成任务失败: %w", err)
	}
	return info.ID, nil
}

// Inspector 暴露 asynq 的检查器，供运维接口查看队列深度与失败任务。
func (c *Client) Inspector(cfg config.RedisConfig) *asynq.Inspector {
	return asynq.NewInspector(redisOpt(cfg))
}
