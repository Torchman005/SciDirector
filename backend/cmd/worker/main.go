// Command worker 是 SciDirector 的 Go 编排与媒体处理进程。
//
// 职责：
//   - 消费 Asynq 队列（主链路 / 单镜头重做 / 合成）；
//   - 通过 gRPC 驱动 Python 多智能体流水线；
//   - 并发调用 ffmpeg 完成归一化与合成。
//
// 它是整个系统里唯一会「跑很久」的进程，因此优雅关闭尤其重要：
// 强杀会让正在渲染的镜头变成孤儿状态（卡在 RENDERING 永远不动）。
package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/itJinYu/SciDirector/backend/internal/ai"
	"github.com/itJinYu/SciDirector/backend/internal/archive"
	"github.com/itJinYu/SciDirector/backend/internal/config"
	"github.com/itJinYu/SciDirector/backend/internal/logging"
	"github.com/itJinYu/SciDirector/backend/internal/media"
	"github.com/itJinYu/SciDirector/backend/internal/queue"
	"github.com/itJinYu/SciDirector/backend/internal/store"
	"github.com/itJinYu/SciDirector/backend/internal/worker"
)

// version 构建时注入。
var version = "0.1.0-dev"

func main() {
	if len(os.Args) > 1 && os.Args[1] == "version" {
		fmt.Println(version)
		return
	}
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "scid-worker 启动失败: %v\n", err)
		os.Exit(1)
	}
}

func run() error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}

	logger := logging.Init(logging.Options{
		Level:   cfg.LogLevel,
		Service: "scid-worker",
		JSON:    !cfg.IsDev(),
	})
	logger.Info("scid-worker 启动中", "version", version, "env", cfg.Env)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	// 媒体工作目录必须可写：提前验证，免得任务跑到合成阶段才发现没权限。
	if err := os.MkdirAll(cfg.Media.WorkDir, 0o755); err != nil {
		return fmt.Errorf("worker: 无法创建媒体工作目录 %s: %w", cfg.Media.WorkDir, err)
	}

	st, err := store.New(ctx, cfg.Redis)
	if err != nil {
		return err
	}
	defer func() { _ = st.Close() }()

	q := queue.NewClient(cfg.Redis, cfg.Queue)
	defer func() { _ = q.Close() }()

	aiClient, err := ai.NewClient(cfg.AI, logger)
	if err != nil {
		return err
	}
	defer func() { _ = aiClient.Close() }()

	// ffmpeg 可用性检查：这是本进程的核心外部依赖，
	// 在启动期发现「镜像里没装 ffmpeg」远比在合成阶段失败划算。
	runner, err := media.NewRunner(cfg.Media)
	if err != nil {
		return fmt.Errorf("worker: 媒体配置非法: %w", err)
	}
	verifyCtx, cancelVerify := context.WithTimeout(ctx, 30*time.Second)
	err = runner.Verify(verifyCtx)
	cancelVerify()
	if err != nil {
		return fmt.Errorf("worker: ffmpeg 环境检查失败: %w", err)
	}
	// 打印 Runner 实际生效的并发上限，而不是配置原值：
	// 配置非法时 Runner 会夹到 1，日志必须反映真正生效的值，
	// 否则排查「并发怎么上不去」时会对着一个从未生效的数字发呆。
	logger.Info("ffmpeg 环境检查通过",
		"max_parallel", runner.MaxParallel(),
		"cmd_timeout", cfg.Media.CommandTimeout.String(),
		"transition", string(runner.Transition().Type),
		"transition_sec", runner.Transition().DurationSec,
		"work_dir", cfg.Media.WorkDir,
	)

	// Python 大脑就绪性探测：不可达时只告警不退出。
	// 理由：容器编排中 worker 常先于 ai 启动，靠 gRPC 惰性重连自动恢复；
	// 若这里直接退出，会陷入「重启 -> ai 还没起来 -> 又退出」的循环。
	healthCtx, cancelHealth := context.WithTimeout(ctx, 10*time.Second)
	if _, err := aiClient.Health(healthCtx); err != nil {
		logger.Warn("AI 大脑暂不可达，将依赖 gRPC 惰性重连", "addr", cfg.AI.Addr, "error", err.Error())
	} else {
		logger.Info("AI 大脑连接正常", "addr", cfg.AI.Addr)
	}
	cancelHealth()

	// 归档后端：配置错误必须在启动期暴露，而不是等到第一个任务合成完才发现
	// 「产物不知道该送去哪」。
	archiver, err := archive.New(archive.Options{
		Backend:        cfg.Archive.Backend,
		LocalDir:       cfg.Archive.LocalDir,
		KeepAll:        cfg.Archive.KeepAll,
		KeepNormalized: cfg.Archive.KeepNormalized,
		S3: archive.S3Options{
			Endpoint:  cfg.Archive.MinioEndpoint,
			AccessKey: cfg.Archive.MinioAccessKey,
			SecretKey: cfg.Archive.MinioSecretKey,
			Bucket:    cfg.Archive.MinioBucket,
			UseSSL:    cfg.Archive.MinioUseSSL,
			Region:    cfg.Archive.MinioRegion,
		},
	}, logger)
	if err != nil {
		return fmt.Errorf("worker: 归档配置非法: %w", err)
	}
	logger.Info("归档后端",
		"backend", archiver.Kind(),
		"enabled", archiver.Enabled(),
		"keep_all", cfg.Archive.KeepAll,
		"keep_normalized", cfg.Archive.KeepNormalized,
	)

	processor := worker.NewProcessor(cfg, st, aiClient, q, runner, archiver, logger)

	asynqServer := queue.NewServer(cfg.Redis, cfg.Queue, logger)
	mux := queue.NewMux()
	worker.RegisterHandlers(mux, processor)

	// Asynq 的 Start 是非阻塞的，因此错误通过 channel 回传主流程。
	serverErr := make(chan error, 1)
	go func() {
		logger.Info("Asynq 消费者启动",
			"concurrency", cfg.Queue.Concurrency,
			"queues", cfg.Queue.Queues,
		)
		if err := asynqServer.Run(mux); err != nil {
			serverErr <- fmt.Errorf("Asynq 服务异常退出: %w", err)
		}
	}()

	select {
	case err := <-serverErr:
		return err
	case <-ctx.Done():
		logger.Info("收到退出信号，等待在途任务收尾（最长 30s）")
	}

	// Shutdown 会停止拉取新任务并等待在途任务结束。
	// 在途任务若超过 ShutdownTimeout 会被强制取消 ctx，
	// 我们的 handler 会因此返回 context.Canceled，任务被重新投递而不是标为失败。
	asynqServer.Shutdown()
	logger.Info("scid-worker 已退出")
	return nil
}
