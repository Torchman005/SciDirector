package domain

import (
	"fmt"
	"strings"
	"time"
)

// allowedTransitions 是**唯一的状态迁移真源**。
//
// 为什么要硬编码而不是让业务代码随意赋值？
//  1. 状态机是本系统正确性的核心。非法迁移（例如 PENDING 直接跳到 APPROVED）
//     通常意味着上游逻辑有 bug，必须尽早暴露而非静默接受。
//  2. 它是可测试的纯函数，不需要 Redis / gRPC 就能验证。
//  3. 前端与文档都由它推导，避免三处漂移。
var allowedTransitions = map[ShotStatus]map[ShotStatus]struct{}{
	StatusPending: {
		StatusGenerating: {}, // 正常入口
		StatusRetrying:   {}, // 重试任务重新入队
		StatusFailed:     {}, // 入队阶段即失败
	},
	StatusGenerating: {
		StatusRendering:     {}, // 代码生成完成，进入渲染
		StatusRetrying:      {}, // 生成失败（LLM 超时 / 语法校验不通过）
		StatusFailed:        {}, // 生成阶段技术性失败
		StatusAwaitingHuman: {}, // 重试耗尽
	},
	StatusRendering: {
		StatusCritiquing:    {}, // 渲染成功，进入审查
		StatusRetrying:      {}, // 渲染失败，带编译错误回灌重写
		StatusFailed:        {}, // 沙盒基础设施故障
		StatusAwaitingHuman: {},
	},
	StatusCritiquing: {
		StatusApproved:      {}, // 审查通过
		StatusRejected:      {}, // 审查不通过
		StatusRetrying:      {}, // 直接重试（审查阶段自身故障后的补救）
		StatusFailed:        {},
		StatusAwaitingHuman: {}, // VLM 不可用降级 -> 转人工
	},
	StatusRejected: {
		StatusRetrying:      {},
		StatusAwaitingHuman: {}, // 重试次数已达上限
	},
	StatusRetrying: {
		StatusGenerating:    {}, // 携带反馈重新生成
		StatusFailed:        {},
		StatusAwaitingHuman: {},
	},
	StatusFailed: {
		StatusRetrying:      {}, // Asynq 重试（基础设施层）
		StatusAwaitingHuman: {}, // 业务层放弃自动重试
	},
	StatusAwaitingHuman: {
		// 人工审核员给出意见后，重新进入重试流程；
		// 人工也可能直接接受现状（例如自行剪辑），因此允许直接 APPROVED。
		StatusRetrying: {},
		StatusApproved: {},
	},
	StatusApproved: {
		// 已通过不等于不可变更：人工可以在审核台要求重做某个已通过的镜头。
		StatusRetrying: {},
	},
}

// CanTransition 报告 from -> to 是否为合法迁移。
func CanTransition(from, to ShotStatus) bool {
	// 幂等：重复写入同一状态（例如重放事件）应当被允许，否则上层需要到处做去重。
	if from == to {
		return true
	}
	dsts, ok := allowedTransitions[from]
	if !ok {
		return false
	}
	_, ok = dsts[to]
	return ok
}

// Transition 校验并返回迁移后的状态。
// 非法迁移返回错误 —— 调用方**必须**处理，禁止忽略。
func Transition(from, to ShotStatus) (ShotStatus, error) {
	if !CanTransition(from, to) {
		return from, fmt.Errorf("domain: 非法状态迁移 %s -> %s", from, to)
	}
	return to, nil
}

// TransitionReason 汇总「这次迁移因何发生」，写入事件流用于追溯。
func TransitionReason(from, to ShotStatus, attempt int, detail string) string {
	var cause string
	switch {
	case from == to:
		cause = "状态重放/幂等写入"
	case to == StatusRetrying && attempt > 1:
		cause = fmt.Sprintf("第 %d 次重试", attempt)
	case to == StatusAwaitingHuman:
		cause = fmt.Sprintf("重试已达上限（%d 次），转人工审核", attempt)
	case to == StatusApproved:
		cause = "审查通过"
	case to == StatusRejected:
		cause = "审查不合格，打回重做"
	default:
		cause = string(to)
	}
	if strings.TrimSpace(detail) != "" {
		cause += "：" + detail
	}
	return cause
}

// MaxAttemptsExceeded 判断某个镜头是否应触发熔断（转人工）。
//
// 这是**成本控制的核心闸门**：没有它，一个永远渲染不好的镜头会无限烧钱。
func MaxAttemptsExceeded(attempt, max int) bool {
	return attempt >= max
}

// NewShot 依据索引与标签构造一个处于 PENDING 的镜头，并完成确定性路由。
// 把「标签 -> 引擎」的映射收口在这里，避免各调用点各写一遍 switch。
func NewShot(jobID string, index int, tag Tag) (*Shot, error) {
	engine, err := EngineForTag(tag)
	if err != nil {
		return nil, err
	}
	return &Shot{
		ShotID:    ShotID(jobID, index),
		JobID:     jobID,
		Index:     index,
		Tag:       tag,
		Engine:    engine,
		Status:    StatusPending,
		Attempt:   0,
		UpdatedAt: time.Now().UTC(),
	}, nil
}

// ShotID 由 jobID 与序号派生稳定 ID。
// 使用「可预测」而非随机 UUID 的理由：人类反馈是按镜头定位的，
// 稳定的 ID 让日志、URL、事件流天然可读可对齐。
func ShotID(jobID string, index int) string {
	return fmt.Sprintf("%s-s%03d", jobID, index)
}
