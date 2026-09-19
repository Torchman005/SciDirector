package httpapi

// 多租户的 HTTP 层：身份解析 + 统一的归属检查入口。
//
// 设计要点见 `domain/tenant.go` 的说明（尤其是「这是归属与隔离，不是身份认证」
// 以及「越权返回 404 而不是 403」这两条）。这里只补充**实现层面**的两个决定：
//
//  1. **解析与校验分开。** `TenantMiddleware` 只负责把租户写进 context；
//     是否放行、租户缺失时是拒绝还是回落到 default，由配置决定。
//  2. **归属检查只有一个入口** `loadJobForTenant`。所有按 job_id 的 handler 都走它，
//     这样「新增了一个接口但忘了做隔离」这件事有一个可被测试钉住的位置。

import (
	"context"
	"errors"
	"net/http"

	"github.com/gin-gonic/gin"

	"github.com/itJinYu/SciDirector/backend/internal/domain"
	"github.com/itJinYu/SciDirector/backend/internal/logging"
	"github.com/itJinYu/SciDirector/backend/internal/store"
)

// ctxKeyTenant 是租户在 gin.Context 与 request context 中的键。
const (
	ginKeyTenant   = "scid_tenant_id"
	ctxKeyTenant   = "scid.tenant_id"
	tenantHeader   = "X-Tenant-ID"
	tenantLogField = "tenant_id"
)

// TenantMiddleware 解析请求声明的租户并注入上下文。
//
// 取值来源是请求头（默认 `X-Tenant-ID`）。这在「可信网关已鉴权并透传身份」的部署里
// 是成立的；**直接暴露到公网则不成立** —— 生产应换成从令牌/证书解析的实现
// （`SCID_TENANT_MODE=required` 会强制要求必须带上，避免静默回落到同一个租户）。
//
// mode 的取值：
//   - "header"（缺省）：读请求头，缺失时回落到 `default`。单租户/本地开发友好。
//   - "required"      ：必须带请求头，否则 400。**多租户部署必须用这个** ——
//     它把「网关照配了但没透传租户」从一个静默的越权风险变成立刻可见的失败：
//     否则所有租户会一起落进 default，彼此看得见对方的任务。
func TenantMiddleware(mode string, header string) gin.HandlerFunc {
	if header == "" {
		header = tenantHeader
	}
	required := mode == "required"

	return func(c *gin.Context) {
		raw := c.GetHeader(header)
		if required && raw == "" {
			abortWith(c, http.StatusBadRequest, ErrCodeBadRequest,
				"缺少租户标识（请求头 "+header+"）", nil)
			return
		}

		tenantID, ok := domain.NormalizeTenantID(raw)
		if !ok {
			abortWith(c, http.StatusBadRequest, ErrCodeBadRequest,
				"租户标识非法（只允许字母/数字/-/_/., 且不超过 64 字符）", nil)
			return
		}

		c.Set(ginKeyTenant, tenantID)

		// 同时放进 request context 与日志上下文：
		//   - request context：供 store、queue 等下游读取（入队时要带进载荷）；
		//   - 日志上下文：让本请求的所有日志都带 tenant_id，多租户下按租户排查才有依据。
		ctx := context.WithValue(c.Request.Context(), ctxKeyTenant, tenantID)
		ctx = logging.WithTenant(ctx, tenantID)
		c.Request = c.Request.WithContext(ctx)

		c.Next()
	}
}

// TenantFromContext 从 request context 读取租户 ID；没有则返回 DefaultTenantID。
//
// **缺失时回落到 default 而不是空串**：空串会让下游到处判空，
// 而「没声明租户」在单租户部署里是完全正常的情况。
func TenantFromContext(ctx context.Context) string {
	if ctx == nil {
		return domain.DefaultTenantID
	}
	if v, ok := ctx.Value(ctxKeyTenant).(string); ok && v != "" {
		return v
	}
	return domain.DefaultTenantID
}

// tenantOf 是 handler 侧读取当前租户的快捷方式。
func tenantOf(c *gin.Context) string {
	if v, ok := c.Get(ginKeyTenant); ok {
		if s, ok := v.(string); ok && s != "" {
			return s
		}
	}
	return TenantFromContext(c.Request.Context())
}

// loadJobForTenant 是**所有按 job_id 访问的 handler 的唯一入口**。
//
// 它同时完成「读取」与「归属检查」，因此调用方没有机会只做前者。
// 越权与不存在都返回同一个 404（错误码也用同一个 NOT_FOUND）——
// 用 403 会确认「这个任务存在」，可被用来枚举有效的 job_id。
func (s *Server) loadJobForTenant(c *gin.Context, jobID string) (*domain.Job, bool) {
	job, err := s.deps.Store.GetJobForTenant(c.Request.Context(), jobID, tenantOf(c))
	if err != nil {
		if errors.Is(err, domain.ErrTenantMismatch) {
			// 与「不存在」走同一条路径，响应完全一致。
			// 日志里**要**区分（否则运维查不到越权尝试），因此显式记一条 warn。
			logging.FromContext(c.Request.Context()).Warn("拒绝了跨租户访问",
				"job_id", jobID, tenantErrorField, err.Error())
			abortWith(c, http.StatusNotFound, ErrCodeNotFound, "任务不存在", nil)
			return nil, false
		}
		if errors.Is(err, store.ErrJobNotFound) {
			abortWith(c, http.StatusNotFound, ErrCodeNotFound, "任务不存在", nil)
			return nil, false
		}
		mapError(c, err)
		return nil, false
	}
	return job, true
}

// tenantErrorField 只用于日志，避免与响应字段混淆。
const tenantErrorField = "reason"
