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
	"fmt"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/prometheus/client_golang/prometheus"

	"github.com/itJinYu/SciDirector/backend/internal/ai"
	"github.com/itJinYu/SciDirector/backend/internal/config"
	"github.com/itJinYu/SciDirector/backend/internal/domain"
	"github.com/itJinYu/SciDirector/backend/internal/logging"
	"github.com/itJinYu/SciDirector/backend/internal/queue"
	"github.com/itJinYu/SciDirector/backend/internal/reconcile"
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
	// ErrCodeRateLimited 用于配额/限流被触发（HTTP 429）。
	// 与 BAD_REQUEST 分开是为了让客户端能区分「请求写错了」与「现在不行，等会儿再来」——
	// 前者重试无用，后者应当退避重试。
	ErrCodeRateLimited = "RATE_LIMITED"
)

// Deps 汇总 HTTP 层所需的外部依赖。
// 用结构体注入而非全局变量，便于测试时替换为零值实现。
type Deps struct {
	Config *config.Config
	Store  *store.Store
	Queue  *queue.Client
	AI     *ai.Client
	Hub    *ws.Hub
	// Inspector 提供队列深度与失败情况。可为 nil（未配置时接口返回 501），
	// 这样 api 进程在没有 Redis 观测权限的部署里仍能正常启动。
	Inspector *queue.Inspector
	// StartedAt 用于 /version 报告进程运行时长。
	StartedAt time.Time
	Version   string
	// Metrics 是 Prometheus 注册表；为 nil 表示未启用（/metrics 返回 501）。
	// 与 Inspector 同样的策略：观测能力缺失不该让网关整个不可用。
	Metrics *prometheus.Registry
	// Reconciler 提供按需状态对账；为 nil 时该接口返回 501。
	//
	// 用接口而不是 *worker.Processor：httpapi 不该依赖 worker 包
	// （worker 背着一整套媒体/归档栈，反向依赖会把编译期耦合拉成一张网）。
	// 接口只声明这里真正要用的那一个方法。
	Reconciler Reconciler
}

// Reconciler 是按需状态对账的能力（由 reconcile.Reconciler 实现）。
//
// 接口定义在**使用方**（httpapi）而不是实现方：这样 httpapi 只依赖
// 一个方法签名，实现放在哪个包、内部怎么组织都与它无关。
type Reconciler interface {
	ReconcileJob(ctx context.Context, jobID string, repair bool) (*reconcile.Outcome, error)
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
	case errors.Is(err, domain.ErrTenantMismatch):
		// 越权与不存在必须给出**完全相同**的响应，包括不携带 detail ——
		// 否则「detail 里写着『任务不属于该租户』」直接确认了这个 job_id 存在，
		// 攻击者可以据此枚举。这正是本轮由路由表用例抓出来的实际缺陷：
		// approve/reject 曾经返回 500 + 那段 detail。
		abortWith(c, http.StatusNotFound, ErrCodeNotFound, "任务不存在", nil)
	case errors.Is(err, store.ErrJobNotFound):
		abortWith(c, http.StatusNotFound, ErrCodeNotFound, "任务不存在", err)
	case asQuotaExceeded(err) != nil:
		// 429 而不是 400/500：语义是「请求本身没错，只是现在不行」，
		// 客户端据此应当退避重试，而不是把它当成 bug 上报。
		q := asQuotaExceeded(err)
		abortWith(c, http.StatusTooManyRequests, ErrCodeRateLimited,
			fmt.Sprintf("在跑任务数已达上限（%d/%d），请等待已有任务完成后再提交", q.Active, q.Limit), nil)
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

// asQuotaExceeded 提取配额错误（`errors.As` 的薄封装，让 switch 里能写得干净些）。
func asQuotaExceeded(err error) *domain.QuotaExceededError {
	var q *domain.QuotaExceededError
	if errors.As(err, &q) {
		return q
	}
	return nil
}
