// Command api 是 SciDirector 的 Go 网关进程。
//
// 职责：REST 接口 + WebSocket 长连接。它**不做**重活：
// 生成任务通过 Asynq 投递给 worker，自己只负责受理、查询与实时推送。
// 这样的分工让 api 可以低成本水平扩容（无状态，只依赖 Redis）。
package main

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/itJinYu/SciDirector/backend/internal/ai"
	"github.com/itJinYu/SciDirector/backend/internal/config"
	"github.com/itJinYu/SciDirector/backend/internal/httpapi"
	"github.com/itJinYu/SciDirector/backend/internal/logging"
	"github.com/itJinYu/SciDirector/backend/internal/queue"
	"github.com/itJinYu/SciDirector/backend/internal/store"
	"github.com/itJinYu/SciDirector/backend/internal/ws"
)

// version 由构建时通过 -ldflags "-X main.version=..." 注入。
// 保留一个可用的默认值，避免本地直接 go run 时输出空串。
var version = "0.1.0-dev"

func main() {
	// 供容器 healthcheck 使用的最小命令，避免为了探针再装一个 HTTP 客户端。
	if len(os.Args) > 1 && os.Args[1] == "version" {
		fmt.Println(version)
		return
	}

	if err := run(); err != nil {
		// 此时 logger 可能尚未初始化，直接写 stderr 保证错误一定可见。
		fmt.Fprintf(os.Stderr, "scid-api 启动失败: %v\n", err)
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
		Service: "scid-api",
		// 容器里输出 JSON 便于采集；本地开发输出文本便于人眼阅读。
		JSON: !cfg.IsDev(),
	})
	logger.Info("scid-api 启动中", "version", version, "env", cfg.Env, "addr", cfg.HTTP.Addr)

	// 收到 SIGINT/SIGTERM 时 ctx 被取消，触发优雅关闭。
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

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

	hub := ws.NewHub(cfg.HTTP.CORSAllowedOrigins, logger)

	// 队列观测。构造本身不连 Redis（Asynq 的 Inspector 是惰性建连的），
	// 因此这里失败也不该阻断 api 启动 —— 观测能力缺失不该让网关不可用。
	inspector := queue.NewInspector(cfg.Redis, cfg.Queue)
	defer func() { _ = inspector.Close() }()
	logger.Info("队列观测已启用", "queues", inspector.Queues())

	deps := httpapi.Deps{
		Config:    cfg,
		Store:     st,
		Queue:     q,
		AI:        aiClient,
		Hub:       hub,
		Inspector: inspector,
		StartedAt: time.Now().UTC(),
		Version:   version,
	}
	router := httpapi.NewRouter(httpapi.NewServer(deps), deps)

	httpSrv := &http.Server{
		Addr:    cfg.HTTP.Addr,
		Handler: router,
		// ReadHeaderTimeout 防 Slowloris：只读请求头就给出时限，不影响上传体积。
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       cfg.HTTP.ReadTimeout,
		// WriteTimeout 必须为 0：WebSocket 是长连接，一旦设置写超时，
		// 到点后连接会被服务端强制掐断，表现为前端定期掉线。
		// 对应的「慢连接」风险由 WS 层自己的 ping/pong 与写超时来兜底。
		WriteTimeout: 0,
		IdleTimeout:  120 * time.Second,
	}

	errCh := make(chan error, 1)
	go func() {
		logger.Info("HTTP 服务开始监听", "addr", cfg.HTTP.Addr)
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errCh <- fmt.Errorf("HTTP 服务异常退出: %w", err)
		}
	}()

	select {
	case err := <-errCh:
		return err
	case <-ctx.Done():
		logger.Info("收到退出信号，开始优雅关闭")
	}

	// 优雅关闭：给在途请求（含正在建立的 WS 会话）一个收尾窗口。
	shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.HTTP.ShutdownTimeout)
	defer cancel()
	if err := httpSrv.Shutdown(shutdownCtx); err != nil {
		return fmt.Errorf("HTTP 优雅关闭失败: %w", err)
	}
	logger.Info("scid-api 已退出")
	return nil
}
