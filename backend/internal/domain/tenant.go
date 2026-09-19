// 多租户：任务归属与隔离（阶段五）。
//
// ## 这个文件实现的是「归属与隔离」，**不是身份认证**
//
// 租户身份由调用方通过请求头声明（默认 `X-Tenant-ID`）。这意味着：
//
//   - 它**能**保证的事情：一个租户拿不到另一个租户的任务 —— 即使猜到 job_id 也不行。
//     归属在任务创建时落库，之后每一次按 job_id 的访问都要比对。
//   - 它**不能**保证的事情：抵御一个能自己伪造请求头的攻击者。
//     在「可信网关已鉴权、并把租户身份透传下来」的部署里这是成立的；
//     直接把这个 API 暴露到公网则不成立。
//
// 把这条区别写进注释而不是含糊地叫「鉴权」，是因为**误以为已经安全**比明确没有安全更危险：
// 前者会让人省掉真正该做的那层（token/SSO），后者不会。
// 上层的接入点是 `TenantResolver`，生产把它换成从令牌/证书里取身份即可，
// 隔离逻辑（本文件与 store 的检查）不需要改。
//
// ## 为什么越权返回 404 而不是 403
//
// 返回 403 等于确认「这个 job_id 存在，只是不属于你」—— 攻击者可以据此枚举出
// 哪些任务 ID 是有效的。因此不存在与无权访问**必须给出同一个响应**。
// 这与「登录失败不区分用户名/密码错误」是同一条思路。

package domain

import (
	"errors"
	"strings"
)

// ErrTenantMismatch 表示任务存在但不属于请求方租户。
//
// 单独定义而不是复用 ErrJobNotFound：store 层需要如实区分这两种情况（便于测试与
// 日志排查），由 **HTTP 层**统一把它们映射成同一个 404 —— 对外不可区分，对内可区分。
var ErrTenantMismatch = errors.New("domain: 任务不属于该租户")

// DefaultTenantID 是未声明租户时的缺省归属。
//
// 用 "default" 而不是空串：空串会让「没传租户」与「传了空串」两种意图混在一起，
// 而这个字段要进日志、进队列载荷、进 Redis —— 到处都要判空是很糟的设计。
const DefaultTenantID = "default"

// tenantIDMaxLen 是租户 ID 的长度上限。
//
// 必须限制：它会进日志字段、Redis 值、gRPC 载荷。不设上限等于把「超长字符串」
// 的传播路径交给了调用方。
const tenantIDMaxLen = 64

// NormalizeTenantID 校验并归一化租户 ID，返回 (结果, 是否合法)。
//
// 合法性规则刻意收得很紧（字母/数字/连字符/下划线）：
// 这个值会出现在日志与各处载荷里，允许任意字符就得在每一处都考虑转义问题，
// 而那些地方**没有一个**会报错，只会悄悄产生歧义。
func NormalizeTenantID(raw string) (string, bool) {
	v := strings.TrimSpace(raw)
	if v == "" {
		return DefaultTenantID, true
	}
	if len(v) > tenantIDMaxLen {
		return "", false
	}
	for _, r := range v {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z',
			r >= '0' && r <= '9', r == '-', r == '_', r == '.':
		default:
			return "", false
		}
	}
	return v, true
}

// JobBelongsTo 报告任务是否属于该租户。
//
// 空租户参数视为 DefaultTenantID；任务上的空归属**也**视为 DefaultTenantID ——
// 这样多租户上线前创建的旧任务仍然可被 default 租户访问，不会因为新增字段而
// 突然全部变成「不存在」。
func JobBelongsTo(job *Job, tenantID string) bool {
	if job == nil {
		return false
	}
	want := tenantID
	if want == "" {
		want = DefaultTenantID
	}
	have := job.TenantID
	if have == "" {
		have = DefaultTenantID
	}
	return have == want
}

// ---------------------------------------------------------------------------
// 配额：按租户限制「同时在跑的任务数」
// ---------------------------------------------------------------------------

// QuotaExceededError 表示该租户的在跑任务数已达上限。
//
// 单独成类型而不是复用某个通用错误：调用方需要把它映射成 429（而不是 400/500），
// 并且消息里要能给出**确切的数字**，好让用户知道该等多久、或者该找谁提额。
type QuotaExceededError struct {
	TenantID string
	Active   int
	Limit    int
}

func (e *QuotaExceededError) Error() string {
	return "domain: 租户 " + e.TenantID + " 的在跑任务数已达上限"
}

// JobActive 报告任务是否仍在占用配额。
//
// 语义是「正在跑」而不是「没结束」：`PARTIAL`（有镜头转人工、其余已完成）**不占用**配额 ——
// 它的流水线已经停了，在等人类，不再消耗渲染资源。若把它也算占用，
// 一个卡着人工审核的任务就会把整个租户挡在门外，而那恰恰是最需要用户
// 还能继续提交别的任务的时候。
//
// 注意：`AWAITING_HUMAN` 是**镜头**状态，不是任务状态；
// 任务层面它体现为 `PARTIAL`。别把两者混起来（我第一版注释就写错了）。
func JobActive(status JobStatus) bool {
	switch status {
	case JobCompleted, JobPartial, JobFailed:
		return false
	default:
		return true
	}
}

// CheckQuota 在创建新任务前检查租户配额。
//
// 纯函数：不碰 IO，因此「什么情况下会拒绝」可以被完整单测 ——
// 配额这种东西一旦算错，表现是「合法请求被随机拒绝」，很难从线上现象反推。
//
// limit <= 0 表示不限制（缺省）。缺省不限制是刻意的：
// 单租户/本地开发不该被一个凭空出现的上限挡住。
func CheckQuota(tenantID string, active, limit int) error {
	if limit <= 0 {
		return nil
	}
	if active >= limit {
		return &QuotaExceededError{TenantID: tenantID, Active: active, Limit: limit}
	}
	return nil
}
