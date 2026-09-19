// Package pbconv 集中处理领域模型与 protobuf 契约之间的双向转换。
//
// 为什么单独成包？
//   - domain 保持零依赖、纯粹；
//   - 契约演进（proto 加字段）时，只需要动这一个文件，两侧代码不必散落修改；
//   - 转换逻辑可以被单测覆盖，避免「枚举拼写错误」这类低级但致命的 bug。
package pbconv

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/itJinYu/SciDirector/backend/internal/domain"
	pb "github.com/itJinYu/SciDirector/backend/internal/pb/scidirector/v1"
)

// ---------------------------------------------------------------------------
// 枚举映射
// ---------------------------------------------------------------------------

var tagToPB = map[domain.Tag]pb.SceneTag{
	domain.TagMath:     pb.SceneTag_SCENE_TAG_MATH,
	domain.TagData:     pb.SceneTag_SCENE_TAG_DATA,
	domain.TagCode:     pb.SceneTag_SCENE_TAG_CODE,
	domain.TagAmbience: pb.SceneTag_SCENE_TAG_AMBIENCE,
}

var pbToTag = func() map[pb.SceneTag]domain.Tag {
	m := make(map[pb.SceneTag]domain.Tag, len(tagToPB))
	for k, v := range tagToPB {
		m[v] = k
	}
	return m
}()

var engineToPB = map[domain.Engine]pb.RenderEngine{
	domain.EngineManim:    pb.RenderEngine_RENDER_ENGINE_MANIM,
	domain.EngineD3:       pb.RenderEngine_RENDER_ENGINE_D3,
	domain.EngineECharts:  pb.RenderEngine_RENDER_ENGINE_ECHARTS,
	domain.EngineCodeAnim: pb.RenderEngine_RENDER_ENGINE_CODE_ANIM,
	domain.EngineStock:    pb.RenderEngine_RENDER_ENGINE_STOCK,
}

var pbToEngine = func() map[pb.RenderEngine]domain.Engine {
	m := make(map[pb.RenderEngine]domain.Engine, len(engineToPB))
	for k, v := range engineToPB {
		m[v] = k
	}
	return m
}()

var statusToPB = map[domain.ShotStatus]pb.ShotStatus{
	domain.StatusPending:       pb.ShotStatus_SHOT_STATUS_PENDING,
	domain.StatusGenerating:    pb.ShotStatus_SHOT_STATUS_GENERATING,
	domain.StatusRendering:     pb.ShotStatus_SHOT_STATUS_RENDERING,
	domain.StatusCritiquing:    pb.ShotStatus_SHOT_STATUS_CRITIQUING,
	domain.StatusApproved:      pb.ShotStatus_SHOT_STATUS_APPROVED,
	domain.StatusRejected:      pb.ShotStatus_SHOT_STATUS_REJECTED,
	domain.StatusRetrying:      pb.ShotStatus_SHOT_STATUS_RETRYING,
	domain.StatusFailed:        pb.ShotStatus_SHOT_STATUS_FAILED,
	domain.StatusAwaitingHuman: pb.ShotStatus_SHOT_STATUS_AWAITING_HUMAN,
}

var pbToStatus = func() map[pb.ShotStatus]domain.ShotStatus {
	m := make(map[pb.ShotStatus]domain.ShotStatus, len(statusToPB))
	for k, v := range statusToPB {
		m[v] = k
	}
	return m
}()

var feedbackSourceToPB = map[domain.FeedbackSource]pb.FeedbackSource{
	domain.FeedbackVLM:    pb.FeedbackSource_FEEDBACK_SOURCE_VLM,
	domain.FeedbackHuman:  pb.FeedbackSource_FEEDBACK_SOURCE_HUMAN,
	domain.FeedbackSystem: pb.FeedbackSource_FEEDBACK_SOURCE_SYSTEM,
}

// TagToPB 转换场景标签；未知标签返回 UNSPECIFIED 而非报错，
// 因为多打一次日志比中断整条流水线更划算（由调用方决定是否视为致命）。
func TagToPB(t domain.Tag) pb.SceneTag {
	if v, ok := tagToPB[t]; ok {
		return v
	}
	return pb.SceneTag_SCENE_TAG_UNSPECIFIED
}

// TagFromPB 转换 proto 场景标签。UNSPECIFIED 返回空串，
// 让调用方能用 `if tag == ""` 明确识别「未设置」，而不是拿到一个看似合法的假值。
func TagFromPB(t pb.SceneTag) domain.Tag {
	if t == pb.SceneTag_SCENE_TAG_UNSPECIFIED {
		return ""
	}
	if v, ok := pbToTag[t]; ok {
		return v
	}
	return domain.Tag(strings.ToLower(strings.TrimPrefix(t.String(), "SCENE_TAG_")))
}

// EngineToPB 转换渲染引擎。
func EngineToPB(e domain.Engine) pb.RenderEngine {
	if v, ok := engineToPB[e]; ok {
		return v
	}
	return pb.RenderEngine_RENDER_ENGINE_UNSPECIFIED
}

// EngineFromPB 转换 proto 渲染引擎；UNSPECIFIED 返回空串。
func EngineFromPB(e pb.RenderEngine) domain.Engine {
	if e == pb.RenderEngine_RENDER_ENGINE_UNSPECIFIED {
		return ""
	}
	if v, ok := pbToEngine[e]; ok {
		return v
	}
	return domain.Engine(strings.ToLower(strings.TrimPrefix(e.String(), "RENDER_ENGINE_")))
}

// StatusToPB 转换镜头状态。
func StatusToPB(s domain.ShotStatus) pb.ShotStatus {
	if v, ok := statusToPB[s]; ok {
		return v
	}
	return pb.ShotStatus_SHOT_STATUS_UNSPECIFIED
}

// StatusFromPB 转换 proto 镜头状态；UNSPECIFIED 返回空串。
// 这一点很关键：上游可能只发「任务级事件」（不带状态），
// 若在此返回 "UNSPECIFIED" 就会把镜头状态污染成一个非法值。
func StatusFromPB(s pb.ShotStatus) domain.ShotStatus {
	if s == pb.ShotStatus_SHOT_STATUS_UNSPECIFIED {
		return ""
	}
	if v, ok := pbToStatus[s]; ok {
		return v
	}
	return domain.ShotStatus(strings.TrimPrefix(s.String(), "SHOT_STATUS_"))
}

// ---------------------------------------------------------------------------
// 实体转换
// ---------------------------------------------------------------------------

// ShotToPB 把领域镜头转为 proto。**只传语义与路径**，绝不传二进制。
func ShotToPB(s *domain.Shot) *pb.ShotSpec {
	if s == nil {
		return nil
	}
	return &pb.ShotSpec{
		ShotId:      s.ShotID,
		Index:       int32(s.Index),
		Narration:   s.Narration,
		VisualBrief: s.VisualBrief,
		Tag:         TagToPB(s.Tag),
		Engine:      EngineToPB(s.Engine),
		DurationSec: s.DurationSec,
		Keywords:    s.Keywords,
		Code:        s.Code,
		Language:    s.Language,
		Meta:        map[string]string{},
	}
}

// ShotFromPB 把 proto 分镜回填为领域镜头（保留既有状态字段）。
// 注意：proto 中不携带 status，状态由 Go 侧状态机独占，避免两处写状态产生分歧。
func ShotFromPB(in *pb.ShotSpec) *domain.Shot {
	if in == nil {
		return nil
	}
	return &domain.Shot{
		ShotID:      in.GetShotId(),
		Index:       int(in.GetIndex()),
		Narration:   in.GetNarration(),
		VisualBrief: in.GetVisualBrief(),
		Tag:         TagFromPB(in.GetTag()),
		Engine:      EngineFromPB(in.GetEngine()),
		DurationSec: in.GetDurationSec(),
		Keywords:    in.GetKeywords(),
		Code:        in.GetCode(),
		Language:    in.GetLanguage(),
		Status:      domain.StatusPending,
		UpdatedAt:   time.Now().UTC(),
	}
}

// ArtifactFromPB 把 proto 产物转为领域产物。
func ArtifactFromPB(in *pb.RenderArtifact) *domain.Artifact {
	if in == nil {
		return nil
	}
	return &domain.Artifact{
		ArtifactID:    in.GetArtifactId(),
		ShotID:        in.GetShotId(),
		VideoPath:     in.GetVideoPath(),
		AudioPath:     in.GetAudioPath(),
		SubtitlePath:  in.GetSubtitlePath(),
		DurationSec:   in.GetDurationSec(),
		Width:         int(in.GetWidth()),
		Height:        int(in.GetHeight()),
		FPS:           int(in.GetFps()),
		Attempt:       int(in.GetAttempt()),
		Engine:        in.GetEngine(),
		FrameSamples:  in.GetFrameSamples(),
		RenderedAt:    unixMs(in.GetRenderedAtUnixMs()),
		RenderCostSec: in.GetRenderCostSec(),
	}
}

// ArtifactToPB 把领域产物转为 proto。
func ArtifactToPB(in *domain.Artifact) *pb.RenderArtifact {
	if in == nil {
		return nil
	}
	return &pb.RenderArtifact{
		ArtifactId:       in.ArtifactID,
		ShotId:           in.ShotID,
		VideoPath:        in.VideoPath,
		AudioPath:        in.AudioPath,
		SubtitlePath:     in.SubtitlePath,
		DurationSec:      in.DurationSec,
		Width:            int32(in.Width),
		Height:           int32(in.Height),
		Fps:              int32(in.FPS),
		Attempt:          int32(in.Attempt),
		Engine:           in.Engine,
		FrameSamples:     in.FrameSamples,
		RenderedAtUnixMs: in.RenderedAt.UnixMilli(),
		RenderCostSec:    in.RenderCostSec,
	}
}

// FeedbackFromPB 把 proto 审查意见转为领域意见。
func FeedbackFromPB(in *pb.CriticFeedback) domain.Feedback {
	if in == nil {
		return domain.Feedback{}
	}
	var src domain.FeedbackSource
	switch in.GetSource() {
	case pb.FeedbackSource_FEEDBACK_SOURCE_HUMAN:
		src = domain.FeedbackHuman
	case pb.FeedbackSource_FEEDBACK_SOURCE_SYSTEM:
		src = domain.FeedbackSystem
	default:
		src = domain.FeedbackVLM
	}
	return domain.Feedback{
		Passed:           in.GetPassed(),
		Score:            in.GetScore(),
		Issues:           in.GetIssues(),
		Suggestions:      in.GetSuggestions(),
		RawResponse:      in.GetRawResponse(),
		Model:            in.GetModel(),
		Source:           src,
		Attempt:          int(in.GetAttempt()),
		LogicScore:       in.GetLogicScore(),
		ReadabilityScore: in.GetReadabilityScore(),
		PacingScore:      in.GetPacingScore(),
		AestheticsScore:  in.GetAestheticsScore(),
		CreatedAt:        unixMs(in.GetCreatedAtUnixMs()),
	}
}

// FeedbackToPB 把领域意见转为 proto，供细粒度 RPC（ReviseShot）回灌。
func FeedbackToPB(in *domain.Feedback) *pb.CriticFeedback {
	if in == nil {
		return nil
	}
	src := pb.FeedbackSource_FEEDBACK_SOURCE_VLM
	if v, ok := feedbackSourceToPB[in.Source]; ok {
		src = v
	}
	return &pb.CriticFeedback{
		Passed:           in.Passed,
		Score:            in.Score,
		Issues:           in.Issues,
		Suggestions:      in.Suggestions,
		RawResponse:      in.RawResponse,
		Model:            in.Model,
		Source:           src,
		Attempt:          int32(in.Attempt),
		LogicScore:       in.LogicScore,
		ReadabilityScore: in.ReadabilityScore,
		PacingScore:      in.PacingScore,
		AestheticsScore:  in.AestheticsScore,
		CreatedAtUnixMs:  in.CreatedAt.UnixMilli(),
	}
}

// EventFromPipeline 把 Python 流式事件转为领域事件，保持事件流字段统一。
func EventFromPipeline(ev *pb.PipelineEvent) domain.Event {
	if ev == nil {
		return domain.Event{}
	}
	e := domain.Event{
		JobID:     ev.GetJobId(),
		ShotID:    ev.GetShotId(),
		ShotIndex: int(ev.GetShotIndex()),
		Node:      ev.GetNode(),
		Status:    StatusFromPB(ev.GetStatus()),
		Message:   ev.GetMessage(),
		Attempt:   int(ev.GetAttempt()),
		Progress:  ev.GetProgress(),
		Error:     ev.GetError(),
		Timestamp: unixMs(ev.GetTsUnixMs()),
	}
	if e.Timestamp.IsZero() {
		e.Timestamp = time.Now().UTC()
	}
	if a := ev.GetArtifact(); a != nil {
		e.Payload = map[string]any{"artifact": ArtifactFromPB(a)}
	}
	if f := ev.GetFeedback(); f != nil {
		fb := FeedbackFromPB(f)
		if e.Payload == nil {
			e.Payload = map[string]any{}
		}
		e.Payload["feedback"] = fb
	}
	if pj := ev.GetPayloadJson(); pj != "" {
		if e.Payload == nil {
			e.Payload = map[string]any{}
		}
		e.Payload["raw"] = pj
		if err := applyPipelinePayload(e.Payload, pj); err != nil {
			// payload_json 是「快变结构」的逃生口，解析失败不该让整条事件失败
			// （那会丢状态迁移）；但也绝不能静默 —— 否则 Python 改了字段名，
			// 这边只会表现为「成本一直是 0」，没有任何报错可查。
			// 因此把错误放进 payload，它会随事件流落进日志与 WS 推送。
			e.Payload["raw_parse_error"] = err.Error()
		}
	}
	return e
}

// pipelinePayload 是 Python 侧 payload_json 的**结构化视图**。
//
// 只声明真正被消费的字段：payload_json 的契约就是「结构可以快速演进」（Agent.md §9），
// 把它整个反序列化成 map[string]any 会让下游开始依赖它的内部形状，
// 等于把一个刻意留松的接口又焊死。这里的每个字段都对应一个明确的下游消费者。
type pipelinePayload struct {
	// 字段名照抄 Python 的线上格式（cost.llm_*），不为了好看而改名：
	// 两侧各用各的命名，只会让「哪边写错了」变成一个需要猜的问题。
	Cost *struct {
		LLMPromptTokens     int `json:"llm_prompt_tokens"`
		LLMCompletionTokens int `json:"llm_completion_tokens"`
		LLMTotalTokens      int `json:"llm_total_tokens"`
		LLMCalls            int `json:"llm_calls"`
	} `json:"cost"`
}

// applyPipelinePayload 把 payload_json 里我们认识的字段转成领域类型放进 payload。
func applyPipelinePayload(dst map[string]any, raw string) error {
	var p pipelinePayload
	if err := json.Unmarshal([]byte(raw), &p); err != nil {
		return err
	}
	if p.Cost != nil {
		// 转成 domain.LLMUsage 而不是匿名结构：worker 侧可直接取用，
		// 且类型上就区分了「上报的用量」与「读取时现算的成本快照」。
		dst["llm_usage"] = domain.LLMUsage{
			PromptTokens:     p.Cost.LLMPromptTokens,
			CompletionTokens: p.Cost.LLMCompletionTokens,
			TotalTokens:      p.Cost.LLMTotalTokens,
			Calls:            p.Cost.LLMCalls,
		}
	}
	return nil
}

// JobProgressToPB 供 REST 或未来的 gRPC 查询复用。
func JobProgressToPB(j *domain.Job) *pb.JobProgress {
	st := j.Stat()
	return &pb.JobProgress{
		JobId:              j.JobID,
		TotalShots:         int32(st.Total),
		ApprovedShots:      int32(st.Approved),
		FailedShots:        int32(st.Failed),
		AwaitingHumanShots: int32(st.AwaitingHuman),
		Progress:           j.ProgressRatio(),
		StartedAtUnixMs:    j.CreatedAt.UnixMilli(),
		UpdatedAtUnixMs:    j.UpdatedAt.UnixMilli(),
	}
}

// unixMs 把毫秒时间戳转为 time.Time；0 值返回零时间（调用方判断）。
func unixMs(ms int64) time.Time {
	if ms <= 0 {
		return time.Time{}
	}
	return time.UnixMilli(ms).UTC()
}

// MustTag 用于测试与提示词构造，非法标签直接 panic（仅限不可恢复的编程错误）。
func MustTag(s string) domain.Tag {
	t := domain.Tag(strings.ToUpper(strings.TrimSpace(s)))
	if !t.Valid() {
		panic(fmt.Sprintf("pbconv: 非法场景标签 %q", s))
	}
	return t
}
