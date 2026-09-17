package httpapi

import (
	"time"

	"github.com/itJinYu/SciDirector/backend/internal/domain"
)

// GenerateRequest 是提交一次科普视频生成请求的请求体。
//
// 校验策略（binding tag）：
//   - 下限防止空/占位脚本浪费一次完整的渲染流水线；
//   - 上限防止有人贴一整本书进来，把 LLM 上下文与费用打爆。
type GenerateRequest struct {
	RawScript         string         `json:"raw_script" binding:"required,min=10,max=20000"`
	TargetDurationSec float64        `json:"target_duration_sec" binding:"omitempty,min=5,max=1800"`
	Locale            string         `json:"locale" binding:"omitempty,oneof=zh-CN en-US ja-JP"`
	StyleGuide        map[string]any `json:"style_guide"`
}

// GenerateResponse 返回任务受理结果。
type GenerateResponse struct {
	JobID     string           `json:"job_id"`
	Status    domain.JobStatus `json:"status"`
	TaskID    string           `json:"task_id,omitempty"` // Asynq 任务 ID，便于在 asynqmon 中定位
	CreatedAt time.Time        `json:"created_at"`
	// WSURL 直接给前端，省去前端自己拼路径的重复逻辑。
	WSURL string `json:"ws_url"`
}

// JobResponse 是任务详情的响应体。
type JobResponse struct {
	Job      *domain.Job    `json:"job"`
	Stat     domain.JobStat `json:"stat"`
	Progress float64        `json:"progress"`
}

// ShotsResponse 只返回分镜数组（审核台的高频轮询端点，避免重复传整份脚本）。
type ShotsResponse struct {
	JobID string         `json:"job_id"`
	Total int            `json:"total"`
	Shots []*domain.Shot `json:"shots"`
}

// EventsResponse 用于事件流增量拉取（WS 不可用时的降级通道）。
type EventsResponse struct {
	JobID   string         `json:"job_id"`
	AfterID int64          `json:"after_id"`
	Events  []domain.Event `json:"events"`
}

// RejectRequest 是人工打回某个镜头的请求体。
//
// Comment 必填且有意义的最小长度限制：这是 HITL 闭环的质量闸门 ——
// 「不好看」这种无信息量的意见无法转成可执行的代码修改，必须拦住。
type RejectRequest struct {
	Comment string `json:"comment" binding:"required,min=2,max=2000"`
}

// RejectResponse 返回打回受理结果。
type RejectResponse struct {
	JobID   string            `json:"job_id"`
	ShotID  string            `json:"shot_id"`
	Status  domain.ShotStatus `json:"status"`
	Attempt int               `json:"attempt"`
	TaskID  string            `json:"task_id,omitempty"`
}

// ApproveResponse 返回人工通过结果。
type ApproveResponse struct {
	JobID  string            `json:"job_id"`
	ShotID string            `json:"shot_id"`
	Status domain.ShotStatus `json:"status"`
	// ComposeEnqueued 表示本次放行让所有镜头都通过了，并且合成任务已入队。
	// 前端据此给出「成片即将开始合成」的提示，而不是让用户对着 100% 干等。
	ComposeEnqueued bool `json:"compose_enqueued"`
}

// PatchShotRequest 是分镜编辑请求体（阶段四：成分镜编辑）。
//
// 两个字段都是**可选**的指针：nil 表示「不改这一项」，
// 这与「改成空字符串」是两件不同的事 —— 后者是「清空画外音」的合法意图。
// 用值类型 + 零值判断会把「清空」误判成「不改」，这是 PATCH 语义最常见的坑。
type PatchShotRequest struct {
	Narration   *string `json:"narration,omitempty" binding:"omitempty,max=2000"`
	VisualBrief *string `json:"visual_brief,omitempty" binding:"omitempty,max=2000"`
	// Redo 为 true 时，改完后立即把该镜头重新渲染一遍。
	// 分开是刻意的：审核员可能只想先修正文案、稍后再统一重渲。
	Redo bool `json:"redo"`
	// Comment 是随重做一起回灌给编码智能体的意见（可选）。
	Comment string `json:"comment,omitempty" binding:"max=2000"`
}

// PatchShotResponse 返回编辑与重做受理结果。
type PatchShotResponse struct {
	JobID   string            `json:"job_id"`
	ShotID  string            `json:"shot_id"`
	Status  domain.ShotStatus `json:"status"`
	Attempt int               `json:"attempt,omitempty"`
	TaskID  string            `json:"task_id,omitempty"`
	// Changed 列出真正被修改的字段，便于前端提示与审计。
	Changed []string `json:"changed"`
}

// HealthResponse 是 /healthz 与 /readyz 的响应体。
type HealthResponse struct {
	Status     string            `json:"status"` // ok / degraded / down
	Version    string            `json:"version"`
	UptimeSec  float64           `json:"uptime_sec"`
	Components map[string]string `json:"components"`
}
