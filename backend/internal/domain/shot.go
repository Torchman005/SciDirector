// Package domain 承载 SciDirector 的领域模型与状态机。
//
// 刻意保持「零外部依赖」（只用标准库）：
//   - 它是全系统语义的锚点，不应被 protobuf / redis / gin 的细节污染；
//   - 与 protobuf 的互转集中在 pbconv.go，便于契约演进时一处修改。
package domain

import (
	"fmt"
	"time"
)

// ---------------------------------------------------------------------------
// 场景标签与渲染引擎
// ---------------------------------------------------------------------------

// Tag 是导演智能体为每个镜头打上的场景标签，决定后续的渲染路由。
type Tag string

const (
	TagMath     Tag = "MATH"     // [数学] -> Manim
	TagData     Tag = "DATA"     // [数据] -> D3 / ECharts
	TagCode     Tag = "CODE"     // [代码] -> 代码高亮动画
	TagAmbience Tag = "AMBIENCE" // [氛围] -> 素材检索 / 渐变占位
)

// AllTags 用于校验与提示词生成，避免在两处硬编码标签集合。
var AllTags = []Tag{TagMath, TagData, TagCode, TagAmbience}

// Valid 报告标签是否是受支持的枚举值。
func (t Tag) Valid() bool {
	for _, x := range AllTags {
		if x == t {
			return true
		}
	}
	return false
}

// Engine 是渲染引擎标识。**标签 -> 引擎的映射是确定性的**，
// 模型只负责打标签，不允许直接指定引擎（见 Agent.md §5.2）。
type Engine string

const (
	EngineManim    Engine = "manim"     // Python + Manim + LaTeX
	EngineD3       Engine = "d3"        // HTML + D3.js，headless 浏览器录制
	EngineECharts  Engine = "echarts"   // HTML + ECharts
	EngineCodeAnim Engine = "code_anim" // 代码高亮打字动画
	EngineStock    Engine = "stock"     // 素材检索 / 程序化渐变
)

// EngineForTag 实现「标签 -> 引擎」的确定性路由。
// 这是整套系统的路由枢纽：任何新增标签都必须在此显式登记，否则返回错误。
func EngineForTag(t Tag) (Engine, error) {
	switch t {
	case TagMath:
		return EngineManim, nil
	case TagData:
		return EngineD3, nil
	case TagCode:
		return EngineCodeAnim, nil
	case TagAmbience:
		return EngineStock, nil
	default:
		return "", fmt.Errorf("domain: 未知场景标签 %q，无法路由到渲染引擎", t)
	}
}

// ---------------------------------------------------------------------------
// 镜头状态机
// ---------------------------------------------------------------------------

// ShotStatus 是单个分镜的生命周期状态。
type ShotStatus string

const (
	StatusPending       ShotStatus = "PENDING"        // 待生成
	StatusGenerating    ShotStatus = "GENERATING"     // 生成中（LLM 写/改代码）
	StatusRendering     ShotStatus = "RENDERING"      // 渲染中（沙盒执行）
	StatusCritiquing    ShotStatus = "CRITIQUING"     // 审查中（VLM 看帧）
	StatusApproved      ShotStatus = "APPROVED"       // 已完成
	StatusRejected      ShotStatus = "REJECTED"       // 语义不合格，已打回
	StatusRetrying      ShotStatus = "RETRYING"       // 重试中
	StatusFailed        ShotStatus = "FAILED"         // 技术性失败
	StatusAwaitingHuman ShotStatus = "AWAITING_HUMAN" // 熔断：等待人工介入
)

// AllShotStatuses 供 UI 与校验使用。
var AllShotStatuses = []ShotStatus{
	StatusPending, StatusGenerating, StatusRendering, StatusCritiquing,
	StatusApproved, StatusRejected, StatusRetrying, StatusFailed, StatusAwaitingHuman,
}

// Terminal 报告该状态是否为终态（不再自动流转）。
// 注意 AWAITING_HUMAN 是「挂起」而非终态：人工给出意见后可继续。
func (s ShotStatus) Terminal() bool {
	return s == StatusApproved
}

// JobStatus 是整个任务的生命周期状态。
type JobStatus string

const (
	JobCreated   JobStatus = "CREATED"
	JobPlanning  JobStatus = "PLANNING"  // 导演智能体拆解脚本
	JobRendering JobStatus = "RENDERING" // 逐镜头生成 + 审查
	JobComposing JobStatus = "COMPOSING" // ffmpeg 合并
	JobCompleted JobStatus = "COMPLETED" // 全部镜头通过并合成成功
	JobPartial   JobStatus = "PARTIAL"   // 有镜头转人工，但其余已完成
	JobFailed    JobStatus = "FAILED"    // 整体失败
)

// ---------------------------------------------------------------------------
// 领域实体
// ---------------------------------------------------------------------------

// Artifact 是一次渲染尝试的产物。只存路径与元数据，绝不内嵌二进制。
type Artifact struct {
	ArtifactID    string    `json:"artifact_id"`
	ShotID        string    `json:"shot_id"`
	VideoPath     string    `json:"video_path"`
	AudioPath     string    `json:"audio_path,omitempty"`
	SubtitlePath  string    `json:"subtitle_path,omitempty"`
	DurationSec   float64   `json:"duration_sec"`
	Width         int       `json:"width"`
	Height        int       `json:"height"`
	FPS           int       `json:"fps"`
	Attempt       int       `json:"attempt"`
	Engine        string    `json:"engine"`
	FrameSamples  []string  `json:"frame_samples,omitempty"`
	RenderedAt    time.Time `json:"rendered_at"`
	RenderCostSec float64   `json:"render_cost_sec"`
}

// FeedbackSource 区分审查意见来自 VLM 还是人类。
// 二者共用同一结构，Coder 侧无需分支处理，简化回灌链路。
type FeedbackSource string

const (
	FeedbackVLM    FeedbackSource = "VLM"
	FeedbackHuman  FeedbackSource = "HUMAN"
	FeedbackSystem FeedbackSource = "SYSTEM" // 沙盒编译报错等技术信息
)

// Feedback 是一条审查/修改意见。
type Feedback struct {
	Passed      bool           `json:"passed"`
	Score       float64        `json:"score"`
	Issues      []string       `json:"issues,omitempty"`
	Suggestions []string       `json:"suggestions,omitempty"` // 必须是可执行的修改指令
	RawResponse string         `json:"raw_response,omitempty"`
	Model       string         `json:"model,omitempty"`
	Source      FeedbackSource `json:"source"`
	Attempt     int            `json:"attempt"`
	// 分维度分数，便于统计「问题主要集中在可读性还是节奏」。
	LogicScore       float64   `json:"logic_score"`
	ReadabilityScore float64   `json:"readability_score"`
	PacingScore      float64   `json:"pacing_score"`
	AestheticsScore  float64   `json:"aesthetics_score"`
	CreatedAt        time.Time `json:"created_at"`
}

// Shot 是一个分镜。它同时是「渲染指令」「状态载体」与「审查对象」。
type Shot struct {
	ShotID      string   `json:"shot_id"`
	JobID       string   `json:"job_id"`
	Index       int      `json:"index"`
	Narration   string   `json:"narration"`    // 画外音 / 字幕
	VisualBrief string   `json:"visual_brief"` // 视觉意图
	Tag         Tag      `json:"tag"`
	Engine      Engine   `json:"engine"`
	DurationSec float64  `json:"duration_sec"`
	Keywords    []string `json:"keywords,omitempty"`
	Code        string   `json:"code,omitempty"`
	Language    string   `json:"language,omitempty"` // python / html+js

	Status    ShotStatus `json:"status"`
	Attempt   int        `json:"attempt"` // 已尝试次数，熔断依据
	Artifact  *Artifact  `json:"artifact,omitempty"`
	Feedbacks []Feedback `json:"feedbacks,omitempty"`
	Error     string     `json:"error,omitempty"`
	UpdatedAt time.Time  `json:"updated_at"`
}

// Job 是一次「脚本 -> 成片」的生成请求。
type Job struct {
	JobID string `json:"job_id"`
	// TenantID 是任务的**归属**（阶段五·多租户）。
	//
	// 在创建时落库、之后不可更改：归属一旦可以改，"谁有权看"就变得不可推理。
	// 旧任务没有这个字段，读取时按 DefaultTenantID 处理（见 JobBelongsTo），
	// 因此加这个字段不会让历史任务突然全部「不存在」。
	TenantID          string         `json:"tenant_id,omitempty"`
	RawScript         string         `json:"raw_script"`
	StyleGuide        map[string]any `json:"style_guide,omitempty"`
	TargetDurationSec float64        `json:"target_duration_sec"`
	Locale            string         `json:"locale"`
	Status            JobStatus      `json:"status"`
	Shots             []*Shot        `json:"shots"`
	Progress          float64        `json:"progress"`
	FinalVideoPath    string         `json:"final_video_path,omitempty"`
	Error             string         `json:"error,omitempty"`
	// LLMUsage 是 Python 上报的 LLM 用量（Go 推不出来，所以必须持久化）。
	// 渲染/配音等可推导项不存这里，读取时由 CostSnapshot 现算，见 cost.go。
	LLMUsage  *LLMUsage `json:"llm_usage,omitempty"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

// Event 是状态迁移事件，写入 Redis 事件流并推送给 WebSocket 客户端。
// 带自增 EventID 是为了支持前端断线重连后「从 last_event_id 重放」。
type Event struct {
	EventID   int64          `json:"event_id"`
	JobID     string         `json:"job_id"`
	ShotID    string         `json:"shot_id,omitempty"`
	ShotIndex int            `json:"shot_index"`
	Node      string         `json:"node,omitempty"` // plan/code/render/critique/revise/compose
	Status    ShotStatus     `json:"status,omitempty"`
	Message   string         `json:"message,omitempty"`
	Attempt   int            `json:"attempt"`
	Progress  float64        `json:"progress"`
	Error     string         `json:"error,omitempty"`
	Payload   map[string]any `json:"payload,omitempty"`
	Timestamp time.Time      `json:"ts"`
}

// Stat 汇总 Job 中各个状态的数量，供指标与 UI 顶栏使用。
type JobStat struct {
	Total         int `json:"total"`
	Approved      int `json:"approved"`
	Failed        int `json:"failed"`
	AwaitingHuman int `json:"awaiting_human"`
	InProgress    int `json:"in_progress"`
}

// Stat 计算任务的分镜统计与整体进度。
// 进度定义：已通过镜头占比（打回/重试中的镜头不计入完成）。
func (j *Job) Stat() JobStat {
	st := JobStat{Total: len(j.Shots)}
	for _, s := range j.Shots {
		switch s.Status {
		case StatusApproved:
			st.Approved++
		case StatusFailed:
			st.Failed++
		case StatusAwaitingHuman:
			st.AwaitingHuman++
		default:
			st.InProgress++
		}
	}
	return st
}

// ProgressRatio 返回 0..1 的整体进度。
//
// 命名为 Ratio 而非 Progress，是因为 Job 上已有同名的 JSON 字段 Progress：
// Go 不允许结构体同时存在同名字段与方法。字段负责序列化，方法负责计算。
func (j *Job) ProgressRatio() float64 {
	if len(j.Shots) == 0 {
		return 0
	}
	st := j.Stat()
	return float64(st.Approved) / float64(st.Total)
}

// FindShot 按 ID 定位镜头，找不到返回 nil（调用方必须判空）。
func (j *Job) FindShot(shotID string) *Shot {
	for _, s := range j.Shots {
		if s.ShotID == shotID {
			return s
		}
	}
	return nil
}
