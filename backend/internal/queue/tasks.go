// Package queue 封装 Asynq 任务队列。
//
// 分层重试的边界（重要）：
//   - Asynq 负责**基础设施层**重试：进程崩溃、网络抖动、Redis 瞬时不可用。
//   - LangGraph 负责**语义层**重试：画面不合格、字号太小、动画太快。
//
// 两者互不掩盖：一个渲染不出画面的镜头不会靠 Asynq 重试变好，
// 而一次 Redis 抖动也不该被当成「内容不合格」记入镜头的 attempt。
package queue

import (
	"encoding/json"
	"fmt"
	"time"

	"github.com/hibiken/asynq"
)

// 任务类型常量。命名规则：task:<聚合>:<动作>，便于在 asynqmon 中归类查看。
const (
	// TaskGenerateJob 是主链路任务：一次「脚本 -> 成片」的完整生成。
	TaskGenerateJob = "task:job:generate"

	// TaskRenderShot 是单镜头任务：人工打回某个镜头时只重跑这一镜。
	// 它的存在是成本控制的关键 —— 打回一镜不应触发全片重渲染。
	TaskRenderShot = "task:shot:render"

	// TaskComposeJob 是合成任务：把所有已通过的镜头合并为成片。
	TaskComposeJob = "task:job:compose"
)

// 队列名。与 config.QueueConfig.Queues 的权重配置对应。
const (
	QueueCritical = "critical"
	QueueDefault  = "default"
	QueueLow      = "low"
)

// GenerateJobPayload 是主链路任务的载荷。
//
// 载荷只放「标识与参数」，不放业务状态：
// 状态一律从 Redis 读取，这样重试时拿到的一定是最新状态而非过期快照。
type GenerateJobPayload struct {
	JobID             string  `json:"job_id"`
	RawScript         string  `json:"raw_script"`
	StyleGuideJSON    string  `json:"style_guide_json,omitempty"`
	TargetDurationSec float64 `json:"target_duration_sec"`
	Locale            string  `json:"locale"`
	// EnqueuedAt 用于观测「任务在队列中排队了多久」。
	EnqueuedAt time.Time `json:"enqueued_at"`
}

// RenderShotPayload 是单镜头重做任务的载荷，主要来源于人类反馈闭环。
type RenderShotPayload struct {
	JobID        string `json:"job_id"`
	ShotID       string `json:"shot_id"`
	Attempt      int    `json:"attempt"`
	HumanComment string `json:"human_comment,omitempty"` // 审核员的自然语言意见
	// TriggeredBy 记录是谁触发的重做：worker（自动）还是 api（人工）。
	// 用于审计与「一次通过率」指标的口径区分。
	TriggeredBy string `json:"triggered_by"`
	// PatchStartSec / PatchEndSec 指定**只重渲这一段**（局部重渲染）。
	//
	// 两者相等或 End <= Start 表示整镜重渲。
	// 只在时间轴可控的引擎上生效（AMBIENCE/HTML）；MATH 会退化为整镜重渲，
	// 由 AI 服务在响应里如实告知，Go 侧据此决定拼接还是整体替换。
	//
	// 典型来源：审核员指出「第 3 秒的坐标轴标签重叠了」，
	// 前端把意见对应的时间点填进来，从而避免重渲整个镜头。
	PatchStartSec float64   `json:"patch_start_sec,omitempty"`
	PatchEndSec   float64   `json:"patch_end_sec,omitempty"`
	EnqueuedAt    time.Time `json:"enqueued_at"`
}

// WantsPartialRender 判断载荷是否请求局部重渲染。
func (p *RenderShotPayload) WantsPartialRender() bool {
	return p.PatchEndSec > p.PatchStartSec && p.PatchStartSec >= 0
}

// ComposeJobPayload 是合成任务的载荷。
type ComposeJobPayload struct {
	JobID      string    `json:"job_id"`
	EnqueuedAt time.Time `json:"enqueued_at"`
}

// Encode 把载荷序列化为 asynq.Payload。
func Encode(v any) ([]byte, error) {
	buf, err := json.Marshal(v)
	if err != nil {
		return nil, fmt.Errorf("queue: 序列化任务载荷失败: %w", err)
	}
	return buf, nil
}

// DecodeGenerateJob 反序列化主链路载荷，字段缺失时返回明确错误。
func DecodeGenerateJob(t *asynq.Task) (*GenerateJobPayload, error) {
	var p GenerateJobPayload
	if err := json.Unmarshal(t.Payload(), &p); err != nil {
		return nil, fmt.Errorf("queue: 解析 %s 载荷失败: %w", t.Type(), err)
	}
	if p.JobID == "" {
		return nil, fmt.Errorf("queue: %s 载荷缺少 job_id", t.Type())
	}
	return &p, nil
}

// DecodeRenderShot 反序列化单镜头载荷。
func DecodeRenderShot(t *asynq.Task) (*RenderShotPayload, error) {
	var p RenderShotPayload
	if err := json.Unmarshal(t.Payload(), &p); err != nil {
		return nil, fmt.Errorf("queue: 解析 %s 载荷失败: %w", t.Type(), err)
	}
	if p.JobID == "" || p.ShotID == "" {
		return nil, fmt.Errorf("queue: %s 载荷缺少 job_id/shot_id", t.Type())
	}
	return &p, nil
}

// DecodeComposeJob 反序列化合成载荷。
func DecodeComposeJob(t *asynq.Task) (*ComposeJobPayload, error) {
	var p ComposeJobPayload
	if err := json.Unmarshal(t.Payload(), &p); err != nil {
		return nil, fmt.Errorf("queue: 解析 %s 载荷失败: %w", t.Type(), err)
	}
	if p.JobID == "" {
		return nil, fmt.Errorf("queue: %s 载荷缺少 job_id", t.Type())
	}
	return &p, nil
}
