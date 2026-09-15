package httpapi

import (
	"crypto/rand"
	"encoding/hex"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/itJinYu/SciDirector/backend/internal/logging"
)

// requestIDHeader 是链路追踪 ID 的传输头。
// 复用业界通用的 X-Request-ID，便于接入既有网关与前端 SDK。
const requestIDHeader = "X-Request-ID"

// TraceMiddleware 为每个请求分配（或沿用）trace_id，并注入 context 与响应头。
//
// 为什么放在最外层：这样后续所有中间件与 handler 的日志都自动带上 trace_id，
// 且前端可以把响应头里的 ID 回传给用户，形成「用户报错 -> 一键定位日志」的闭环。
func TraceMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		traceID := c.GetHeader(requestIDHeader)
		if traceID == "" {
			traceID = newID("tr")
		}
		c.Set("trace_id", traceID)
		c.Header(requestIDHeader, traceID)

		ctx := logging.WithTrace(c.Request.Context(), traceID)
		c.Request = c.Request.WithContext(ctx)

		c.Next()
	}
}

// RecoveryMiddleware 捕获 panic，转成 500 而不是让整个进程退出。
//
// 为什么不用 gin.Recovery()：它只打印堆栈到 stderr，无法进结构化日志、
// 也无法与 trace_id 关联，在生产排查时价值很低。
func RecoveryMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		defer func() {
			if r := recover(); r != nil {
				logging.FromContext(c.Request.Context()).Error("HTTP handler panic",
					"path", c.FullPath(),
					"method", c.Request.Method,
					"panic", r,
				)
				abortWith(c, http.StatusInternalServerError, ErrCodeInternal, "服务内部错误", nil)
			}
		}()
		c.Next()
	}
}

// AccessLogMiddleware 记录访问日志。
//
// 只在结束时打一条，包含状态码与耗时；对 WS 路由额外标注，因为它的「耗时」
// 等于连接存活时长，不能用常规阈值判断慢请求。
func AccessLogMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		start := time.Now()
		c.Next()
		elapsed := time.Since(start)

		lg := logging.FromContext(c.Request.Context())
		attrs := []any{
			"method", c.Request.Method,
			"path", c.FullPath(),
			"status", c.Writer.Status(),
			"elapsed_ms", elapsed.Milliseconds(),
			"client_ip", c.ClientIP(),
		}
		switch {
		case c.Writer.Status() >= 500:
			lg.Error("HTTP 访问", attrs...)
		case c.Writer.Status() >= 400:
			lg.Warn("HTTP 访问", attrs...)
		default:
			lg.Info("HTTP 访问", attrs...)
		}
	}
}

// CORSMiddleware 处理跨域。允许列表为空表示放开（仅 dev）。
func CORSMiddleware(allowedOrigins []string) gin.HandlerFunc {
	allowAll := len(allowedOrigins) == 0
	allowed := make(map[string]struct{}, len(allowedOrigins))
	for _, o := range allowedOrigins {
		allowed[strings.TrimSpace(o)] = struct{}{}
	}

	return func(c *gin.Context) {
		origin := c.GetHeader("Origin")
		if origin != "" {
			_, ok := allowed[origin]
			if allowAll || ok {
				c.Header("Access-Control-Allow-Origin", origin)
				// 带上 Vary，避免 CDN/代理把某个来源的响应错误地缓存给其他来源。
				c.Header("Vary", "Origin")
				c.Header("Access-Control-Allow-Credentials", "true")
				c.Header("Access-Control-Allow-Headers", "Content-Type, Authorization, "+requestIDHeader)
				c.Header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
				c.Header("Access-Control-Expose-Headers", requestIDHeader)
			}
		}
		if c.Request.Method == http.MethodOptions {
			c.AbortWithStatus(http.StatusNoContent)
			return
		}
		c.Next()
	}
}

// newID 生成带前缀的随机 ID。
//
// 用 crypto/rand 而非 math/rand：job_id 会出现在 URL 中，
// 可预测的 ID 让未授权访问变得容易（虽然后续会加鉴权，但这是低成本的纵深防御）。
func newID(prefix string) string {
	b := make([]byte, 8) // 64 位随机，碰撞概率可忽略
	if _, err := rand.Read(b); err != nil {
		// 随机源不可用时退化为时间戳，保证功能可用（可用性优先于 ID 随机性）。
		return prefix + "-" + time.Now().UTC().Format("20060102150405.000000")
	}
	return prefix + "-" + hex.EncodeToString(b)
}

// NewJobID 生成任务 ID。对外导出以便 worker 与测试复用同一规则。
func NewJobID() string { return newID("job") }
