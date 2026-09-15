// Package httpapi 是 Go 层的对外 REST 接口（Gin）。
//
// 分层约定：
//   - handler 只做「参数校验 -> 调用领域/仓储 -> 组装响应」，不写业务规则；
//   - 业务规则（状态机、熔断、路由）属于 domain / worker；
//   - 所有错误都通过 apierror 统一映射为稳定的 HTTP 状态码与错误码，
//     让前端能基于错误码而非字符串做处理。
package httpapi

import (
	"context"
	"errors"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/itJinYu/SciDirector/backend/internal/ai"
	"github.com/itJinYu/SciDirector/backend/internal/config"
	"github.com/itJinYu/SciDirector/backend/internal/logging"
	"github.com/itJinYu/SciDirector/backend/internal/queue"
	"github.com/itJinYu/SciDirector/backend/internal/store"
	"github.com/itJinYu/SciDirector/backend/internal/ws"
)

// APIError 是统一的错误响应体。
type APIError struct {
	Code    string `json:"code"`             // 稳定错误码，前端据此分支
	Message string `json:"message"`          // 人类可读说明
	Detail  string `json:"detail,omitempty"` // 调试细节（生产可按需隐藏）
	TraceID string `json:"trace_id,omitempty"`
}

// 错误码常量。新增错误码必须同步 docs/API.md。
const (
	ErrCodeBadRequest   = "BAD_REQUEST"
	ErrCodeNotFound     = "NOT_FOUND"
	ErrCodeConflict     = "CONFLICT"
	ErrCodeUpstream     = "UPSTREAM_UNAVAILABLE"
	ErrCodeInternal     = "INTERNAL"
	ErrCodeUnauthorized = "UNAUTHORIZED"
)

// Deps 汇总 HTTP 层所需的外部依赖。
// 用结构体注入而非全局变量，便于测试时替换为零值实现。
type Deps struct {
	Config *config.Config
	Store  *store.Store
	Queue  *queue.Client
	AI     *ai.Client
	Hub    *ws.Hub
	// StartedAt 用于 /version 报告进程运行时长。
	StartedAt time.Time
	Version   string
}

// abortWith 统一地写出错误响应并终止后续 handler。
func abortWith(c *gin.Context, status int, code, msg string, cause error) {
	body := APIError{Code: code, Message: msg}
	if cause != nil && body.Detail == "" {
		body.Detail = cause.Error()
	}
	if v, ok := c.Get("trace_id"); ok {
		if s, ok := v.(string); ok {
			body.TraceID = s
		}
	}
	// 5xx 用 Error 级别，4xx 用 Warn：避免客户端参数错误淹没真实的服务端故障告警。
	if status >= 500 {
		logging.FromContext(c.Request.Context()).Error("HTTP 请求失败",
			"path", c.FullPath(), "status", status, "code", code, "error", body.Detail)
	} else {
		logging.FromContext(c.Request.Context()).Warn("HTTP 请求被拒绝",
			"path", c.FullPath(), "status", status, "code", code, "reason", body.Message)
	}
	c.AbortWithStatusJSON(status, body)
}

// mapError 把仓储/上游错误映射为 HTTP 状态码。
// 集中映射的好处：不会出现「同一个错误在不同 handler 里返回不同状态码」。
func mapError(c *gin.Context, err error) {
	switch {
	case errors.Is(err, store.ErrJobNotFound):
		abortWith(c, http.StatusNotFound, ErrCodeNotFound, "任务不存在", err)
	case errors.Is(err, store.ErrJobConflict):
		abortWith(c, http.StatusConflict, ErrCodeConflict, "任务正被其他请求修改，请稍后重试", err)
	case errors.Is(err, ai.ErrUnavailable):
		abortWith(c, http.StatusServiceUnavailable, ErrCodeUpstream, "AI 服务暂不可用", err)
	case errors.Is(err, context.DeadlineExceeded):
		abortWith(c, http.StatusGatewayTimeout, ErrCodeUpstream, "上游调用超时", err)
	default:
		abortWith(c, http.StatusInternalServerError, ErrCodeInternal, "服务内部错误", err)
	}
}

// respondOK 是成功响应的统一封装。
func respondOK(c *gin.Context, data any) {
	c.JSON(http.StatusOK, gin.H{"ok": true, "data": data})
}

// respondAccepted 用于「已受理但结果异步产生」的场景（如提交生成任务）。
func respondAccepted(c *gin.Context, data any) {
	c.JSON(http.StatusAccepted, gin.H{"ok": true, "data": data})
}
