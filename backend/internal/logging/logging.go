// Package logging 提供统一的结构化日志能力。
//
// 目标：让 Go 与 Python 两侧的日志字段完全对齐（trace_id / job_id / shot_id /
// attempt / node），这样在同一个采集管道里可以直接按 job_id 串联整条链路。
package logging

import (
	"context"
	"log/slog"
	"os"
	"strings"
)

// 上下文字段键名。跨语言对齐，Python 侧使用同名 key。
const (
	FieldTraceID = "trace_id"
	FieldJobID   = "job_id"
	FieldShotID  = "shot_id"
	FieldAttempt = "attempt"
	FieldNode    = "node"
	FieldTaskID  = "task_id"
)

type ctxKey string

const (
	ctxKeyTraceID ctxKey = "logging.trace_id"
	ctxKeyJobID   ctxKey = "logging.job_id"
)

// Options 控制日志后端行为。
type Options struct {
	Level   string
	Service string // 服务名，写入每条日志的 service 字段
	JSON    bool   // true=JSON（生产/容器），false=文本（本地可读）
}

// Init 构建全局 logger 并设置为 slog 默认值。
func Init(opts Options) *slog.Logger {
	var lvl slog.Level
	switch strings.ToLower(opts.Level) {
	case "debug":
		lvl = slog.LevelDebug
	case "warn", "warning":
		lvl = slog.LevelWarn
	case "error":
		lvl = slog.LevelError
	default:
		lvl = slog.LevelInfo
	}

	handlerOpts := &slog.HandlerOptions{
		Level: lvl,
		// 把 source 位置信息也带上，长链路排查时非常省事。
		AddSource: lvl == slog.LevelDebug,
	}

	var h slog.Handler
	if opts.JSON {
		h = slog.NewJSONHandler(os.Stdout, handlerOpts)
	} else {
		h = slog.NewTextHandler(os.Stdout, handlerOpts)
	}

	logger := slog.New(h).With(slog.String("service", opts.Service))
	slog.SetDefault(logger)
	return logger
}

// WithTrace 把 trace_id 注入 context，供后续所有日志自动携带。
func WithTrace(ctx context.Context, traceID string) context.Context {
	return context.WithValue(ctx, ctxKeyTraceID, traceID)
}

// WithJob 把 job_id 注入 context。
func WithJob(ctx context.Context, jobID string) context.Context {
	return context.WithValue(ctx, ctxKeyJobID, jobID)
}

// FromContext 返回一个已附加 context 中追踪字段的 logger。
// 约定：所有跨函数边界的日志都用 FromContext(ctx).Info(...)，而不是 slog.Info(...)。
func FromContext(ctx context.Context) *slog.Logger {
	l := slog.Default()
	if ctx == nil {
		return l
	}
	if v, ok := ctx.Value(ctxKeyTraceID).(string); ok && v != "" {
		l = l.With(slog.String(FieldTraceID, v))
	}
	if v, ok := ctx.Value(ctxKeyJobID).(string); ok && v != "" {
		l = l.With(slog.String(FieldJobID, v))
	}
	return l
}
