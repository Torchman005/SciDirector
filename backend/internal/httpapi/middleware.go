package httpapi

import (
	"crypto/rand"
	"encoding/hex"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"go.opentelemetry.io/otel/codes"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
	"go.opentelemetry.io/otel/trace"

	"github.com/itJinYu/SciDirector/backend/internal/logging"
	"github.com/itJinYu/SciDirector/backend/internal/obs"
)

// isProbePath 判断是否为高频的机器探针路径（抓取/健康检查）。
//
// 用显式白名单而不是正则：这类路径很少、且改动需要理由，
// 一个宽泛的正则迟早会把业务接口也匹配进去，而那时链路会**静默变少**。
func isProbePath(rawPath string) bool {
	switch rawPath {
	case "/metrics", "/healthz", "/readyz", "/version":
		return true
	default:
		return false
	}
}

// requestIDHeader 是链路追踪 ID 的传输头。
// 复用业界通用的 X-Request-ID，便于接入既有网关与前端 SDK。
const requestIDHeader = "X-Request-ID"

// TraceMiddleware 为每个请求建立 span，并把 trace_id 注入 context、响应头与日志。
//
// 为什么放在最外层：这样后续所有中间件与 handler 的日志都自动带上 trace_id，
// 且前端可以把响应头里的 ID 回传给用户，形成「用户报错 -> 一键定位日志」的闭环。
//
// ## 与 OpenTelemetry 的关系（重要）
//
// 这个中间件同时承担 OTel 的**服务端 span**职责，而不是另挂一个 otelgin。
// 原因是本项目已有自己的 trace_id 语义（`tr-…` 前缀、X-Request-ID 头、日志字段），
// 若再叠一层自动埋点，就会出现**两个都叫 trace_id 的值**：
// 日志里写一个、Tempo 里存另一个，于是「拿日志里的 ID 去查链路」必然查不到 ——
// 而这类不一致不会有任何报错，只会让人误以为「链路没采到」。
//
// 因此这里**只保留一个来源**：span 的 trace ID 就是日志与响应头里的 trace_id。
//   - 上游带 `traceparent` 时沿用其链路（真正意义上的分布式追踪）；
//   - 否则新建一条根链路。
//
// 响应头优先回 `traceparent` 的 trace ID，格式与本项目原有的 `tr-` 前缀不同 ——
// 这是刻意的：前端/网关拿到的是**可用来查 Tempo 的**ID，不再是内部随机串。
func TraceMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		// 抓取端点不建 span。Prometheus 每 5 秒抓一次，每条都留一个 trace
		// 会把真正的业务链路淹掉 —— 查 trace 时看到的全是 /metrics，
		// 这种噪声会让人干脆不再用链路功能。健康探针同理。
		if isProbePath(c.Request.URL.Path) {
			c.Next()
			return
		}

		// 先按 W3C 规范尝试从上游（网关/前端/其他服务）恢复链路上下文。
		ctx := obs.ExtractTraceparent(c.Request.Context(), c.GetHeader(obs.TraceparentHeader))

		ctx, span := obs.Tracer("httpapi").Start(ctx, c.FullPath(),
			trace.WithSpanKind(trace.SpanKindServer),
			trace.WithAttributes(
				semconv.HTTPRequestMethodKey.String(c.Request.Method),
				semconv.URLPath(c.Request.URL.Path),
			),
		)
		defer span.End()

		traceID := obs.TraceIDFromContext(ctx)
		if traceID == "" {
			// 理论上不会发生（Start 一定给出 trace ID）；留一条兜底是为了
			// 让「日志里 trace_id 为空」这件事不会静默出现。
			traceID = newID("tr")
		}

		c.Set("trace_id", traceID)
		c.Set(obs.SpanContextKey, span.SpanContext())
		// 回传可查 Tempo 的 traceparent，而不是内部随机串。
		c.Header(requestIDHeader, traceID)
		if tp := obs.InjectTraceparent(ctx); tp != "" {
			c.Header(obs.TraceparentHeader, tp)
		}

		ctx = logging.WithTrace(ctx, traceID)

		// **必须在 c.Next() 之前**把带 span 的 ctx 装回请求。
		// 放到 c.Next() 之后（我第一版就是这么写的）时，handler 里
		// `c.Request.Context()` 拿到的仍是**没有 span 的**上下文 ——
		// 于是入队时 traceparent 为空，worker 那条链路断成两棵树。
		// 这个错误很隐蔽：两侧各自都有完整可看的 trace，只是不在一棵树上；
		// 靠 `scidirector.trace_continued=false` 这个标记才能一眼看出。
		c.Request = c.Request.WithContext(ctx)

		c.Next()

		// span 的状态在 handler 跑完之后才完整：把 HTTP 状态码与错误补上，
		// 否则 Tempo 里所有请求看起来都是成功的。
		span.SetAttributes(semconv.HTTPResponseStatusCode(c.Writer.Status()))
		if c.Writer.Status() >= 500 {
			span.SetStatus(codes.Error, http.StatusText(c.Writer.Status()))
		}
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

// CORSMiddleware 处理跨域。允许列表为空、或列表中含 `*`，都表示放开（仅 dev）。
//
// `*` 必须显式识别 —— 与 WebSocket 的 CheckOrigin 是同一个坑：
// 把 `*` 当普通字面来源比对，「想全放开」的部署反而一个 CORS 头都拿不到。
// 这里**回显实际 Origin** 而不是写死 `*`：只有响应不是字面 `*` 时，
// 才能与 `Access-Control-Allow-Credentials: true` 共存。
func CORSMiddleware(allowedOrigins []string) gin.HandlerFunc {
	allowAll := len(allowedOrigins) == 0
	allowed := make(map[string]struct{}, len(allowedOrigins))
	for _, o := range allowedOrigins {
		o = strings.TrimSpace(o)
		if o == "*" {
			allowAll = true
			continue
		}
		allowed[o] = struct{}{}
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
