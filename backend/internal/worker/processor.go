// Package worker 实现 Asynq 任务的业务处理逻辑，是 Go 侧的核心编排层。
//
// 三条链路：
//  1. HandleGenerateJob —— 主链路。以**服务端流式**方式驱动 Python 的 LangGraph，
//     把每个节点事件落到 Redis 并实时广播给前端。
//  2. HandleRenderShot  —— 人类反馈闭环。只重做被打回的那一个镜头。
//  3. HandleComposeJob  —— 媒体合成。归一化 + concat + 混流，产出成片（见 handlers.go）。
//
// 统一约定：**任何会改变任务状态的操作，都必须同时 (a) 走状态机校验，
// (b) 追加事件流**。事件流的 Publish 由仓储层完成，api 侧订阅后推给浏览器，
// 从而保证「事件 -> 状态 -> 广播」三者永远一致。
package worker

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"path/filepath"
	"time"

	"github.com/hibiken/asynq"

	"github.com/itJinYu/SciDirector/backend/internal/ai"
	"github.com/itJinYu/SciDirector/backend/internal/config"
	"github.com/itJinYu/SciDirector/backend/internal/domain"
	"github.com/itJinYu/SciDirector/backend/internal/logging"
	"github.com/itJinYu/SciDirector/backend/internal/media"
	pb "github.com/itJinYu/SciDirector/backend/internal/pb/scidirector/v1"
	"github.com/itJinYu/SciDirector/backend/internal/pbconv"
	"github.com/itJinYu/SciDirector/backend/internal/queue"
	"github.com/itJinYu/SciDirector/backend/internal/store"
)

// Processor 汇总 worker 的全部依赖。所有 handler 都是它的方法。
type Processor struct {
	cfg   *config.Config
	store *store.Store
	ai    *ai.Client
	q     *queue.Client
	media *media.Runner
	log   *slog.Logger
}

// NewProcessor 构造处理器。
//
// 注意这里**没有** WebSocket Hub：worker 与 api 可能位于不同进程/容器，
// 把事件推到 worker 进程内的 Hub 对前端毫无意义。
// 事件统一写入 Redis 事件流（AppendEvent 内含 Publish），
// 由 api 侧按任务订阅后扇出 —— 这条路径在单进程与多进程部署下行为一致。
func NewProcessor(
	cfg *config.Config,
	st *store.Store,
	aiClient *ai.Client,
	q *queue.Client,
	runner *media.Runner,
	logger *slog.Logger,
) *Processor {
	return &Processor{cfg: cfg, store: st, ai: aiClient, q: q, media: runner, log: logger}
}

// ---------------------------------------------------------------------------
// 主链路：Job 生成
// ---------------------------------------------------------------------------

// HandleGenerateJob 处理一次完整的生成任务。
//
// 关键点：这是一个**长时间运行的流式消费**，可能持续数十分钟。因此：
//   - 必须响应 ctx 取消（Asynq 的优雅关闭 / 任务超时都会取消 ctx）；
//   - 单点失败应尽量转化为任务内的状态（转人工），而不是让整个任务崩掉。
func (p *Processor) HandleGenerateJob(ctx context.Context, task GenerateTask) error {
	jobID := task.Payload.JobID
	ctx = logging.WithJob(ctx, jobID)
	lg := logging.FromContext(ctx).With("job_id", jobID)

	job, err := p.store.GetJob(ctx, jobID)
	if err != nil {
		// 任务不存在说明状态已过期或被清理，重试没有意义 —— 返回 nil 让 Asynq 归档。
		if errors.Is(err, store.ErrJobNotFound) {
			lg.Warn("任务不存在，忽略该队列消息")
			return nil
		}
		return fmt.Errorf("worker: 读取任务失败: %w", err)
	}

	lg.Info("开始执行生成任务",
		"target_duration_sec", job.TargetDurationSec,
		"locale", job.Locale,
	)

	// 状态推进到 PLANNING 并广播。
	if err := p.transitionJob(ctx, jobID, domain.JobPlanning, "plan", 0,
		"导演智能体正在拆解脚本…", nil); err != nil {
		lg.Warn("更新任务状态失败", "error", err.Error())
	}

	req := &pb.RunPipelineRequest{
		JobId:              jobID,
		RawScript:          job.RawScript,
		StyleGuideJson:     mustJSON(job.StyleGuide),
		TargetDurationSec:  job.TargetDurationSec,
		MaxAttemptsPerShot: int32(p.cfg.Pipeline.ShotMaxAttempts),
		Locale:             job.Locale,
		// 用 job_id 作为 LangGraph 的线程 ID：两侧天然对齐，便于对账与断点恢复。
		CheckpointThreadId: jobID,
	}

	// 消费事件流。onEvent 返回错误会主动终止流。
	err = p.ai.RunPipeline(ctx, req, func(ev *pb.PipelineEvent) error {
		return p.applyPipelineEvent(ctx, jobID, ev)
	})

	if err != nil {
		// 上下文取消（进程关闭 / 任务超时）不算业务失败：
		// 保留任务状态，让 Asynq 的重试机制重新投递，而不是标记为 FAILED。
		if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			lg.Warn("生成任务被中断，等待重新投递", "error", err.Error())
			return err
		}
		// 真实失败：把错误写进任务，并把仍在进行中的镜头标记为失败，避免僵尸状态。
		lg.Error("生成任务失败", "error", err.Error())
		_, _ = p.store.UpdateJob(ctx, jobID, func(j *domain.Job) error {
			j.Status = domain.JobFailed
			j.Error = err.Error()
			for _, s := range j.Shots {
				if !s.Status.Terminal() && s.Status != domain.StatusAwaitingHuman {
					s.Status = domain.StatusFailed
					s.Error = err.Error()
				}
			}
			return nil
		})
		_, _ = p.emit(ctx, &domain.Event{
			JobID: jobID, Node: "pipeline", Message: "任务失败：" + err.Error(),
			Error: err.Error(), Timestamp: time.Now().UTC(),
		})

		// 未实现属于**永远不可能成功**的错误：跳过 Asynq 重试直接归档。
		//
		// 不这么做的话，一个"服务端还没实现这个能力"的调用会被按指数退避
		// 重试到上限（默认 5 次），既浪费资源，又会在告警里制造一批必然
		// 重复的噪声，掩盖真正的故障。任务状态已在上面落成 FAILED，
		// 前端依然能看到明确的原因。
		if errors.Is(err, ai.ErrNotImplemented) {
			lg.Warn("该能力尚未实现，跳过重试直接归档", "error", err.Error())
			return fmt.Errorf("%w: %w", asynq.SkipRetry, err)
		}
		return err
	}

	// 流水线结束：根据镜头最终状态决定是进入合成还是标记为「部分完成」。
	final, err := p.store.UpdateJob(ctx, jobID, func(j *domain.Job) error {
		st := j.Stat()
		switch {
		case st.Total > 0 && st.Approved == st.Total:
			j.Status = domain.JobComposing
		case st.AwaitingHuman > 0:
			// 有镜头熔断转人工：整体标记为「部分完成」，等待人工处理。
			// 不标记 FAILED —— 大部分镜头可能是好的，整片仍有交付价值。
			j.Status = domain.JobPartial
		default:
			j.Status = domain.JobFailed
			if j.Error == "" {
				j.Error = "流水线结束但没有可交付的镜头"
			}
		}
		return nil
	})
	if err != nil {
		return err
	}

	lg.Info("生成流水线结束", "status", final.Status, "progress", final.ProgressRatio())

	if final.Status == domain.JobComposing {
		if _, err := p.q.EnqueueComposeJob(ctx, &queue.ComposeJobPayload{
			JobID: jobID, EnqueuedAt: time.Now().UTC(),
		}); err != nil {
			// 合成入队失败不致命：任务状态已是 COMPOSING，可由人工或补偿任务重投。
			lg.Error("合成任务入队失败", "error", err.Error())
			return err
		}
	}
	return nil
}

// applyPipelineEvent 把一条 Python 事件应用到本地状态。
//
// 这是跨语言状态同步的**唯一收口点**：所有由流水线驱动的 Job 修改都必须经过这里，
// 从而保证「事件 -> 状态」永远一致，也便于将来加对账逻辑。
func (p *Processor) applyPipelineEvent(ctx context.Context, jobID string, ev *pb.PipelineEvent) error {
	domainEvent := pbconv.EventFromPipeline(ev)
	domainEvent.JobID = jobID

	// 1) 先更新任务/镜头状态（在分布式锁内完成，避免与其他 worker 竞争）。
	job, err := p.store.UpdateJob(ctx, jobID, func(j *domain.Job) error {
		// 导演节点会一次性带出分镜表，需要在此时把镜头**创建**出来。
		if ev.GetNode() == "plan" && ev.GetPayloadJson() != "" {
			if perr := syncShotsFromPayload(j, ev.GetPayloadJson()); perr != nil {
				// 分镜表解析失败是严重问题（后续无从渲染），但不能静默吞掉：
				// 记入任务错误字段，同时继续应用状态变更。
				j.Error = "解析分镜表失败: " + perr.Error()
			}
		}

		// 没有 shot_id 的事件是任务级事件（例如全片进度），只更新任务状态。
		if ev.GetShotId() == "" {
			j.UpdatedAt = time.Now().UTC()
			return nil
		}

		shot := j.FindShot(ev.GetShotId())
		if shot == nil {
			// 事件引用了未知镜头：通常是导演节点的事件先于分镜表同步到达。
			// 这里不报错，仅等待 plan 事件补齐。
			return nil
		}

		if next := pbconv.StatusFromPB(ev.GetStatus()); next != "" {
			if _, terr := domain.Transition(shot.Status, next); terr != nil {
				// 边界宽容策略：Python 是语义进度的产出方，若它报出一个我们的
				// 迁移表尚未覆盖的路径，说明表不完整而不是事件错误。
				// 记录告警后接受新状态，避免整条流水线因状态表不全而卡死。
				logging.FromContext(ctx).Warn("收到未经登记的状态迁移，已接受",
					"job_id", jobID, "shot_id", shot.ShotID,
					"from", string(shot.Status), "to", string(next),
				)
			}
			shot.Status = next
		}
		if a := ev.GetAttempt(); a > 0 {
			shot.Attempt = int(a)
		}
		if art := pbconv.ArtifactFromPB(ev.GetArtifact()); art != nil {
			shot.Artifact = art
		}
		if fb := ev.GetFeedback(); fb != nil {
			shot.Feedbacks = append(shot.Feedbacks, pbconv.FeedbackFromPB(fb))
		}
		if e := ev.GetError(); e != "" {
			shot.Error = e
		}
		shot.UpdatedAt = time.Now().UTC()

		// 任务整体状态跟随镜头进度推进。
		if j.Status == domain.JobPlanning {
			j.Status = domain.JobRendering
		}
		j.UpdatedAt = time.Now().UTC()
		return nil
	})
	if err != nil {
		if errors.Is(err, store.ErrJobNotFound) {
			return nil
		}
		return err
	}
	domainEvent.Progress = job.ProgressRatio()

	// 2) 落事件流（内含 Publish，api 侧订阅后推给浏览器）。
	_, err = p.emit(ctx, &domainEvent)
	return err
}

// syncShotsFromPayload 解析导演智能体返回的分镜表快照并同步进任务。
//
// 跨语言约定：plan 节点的 PipelineEvent.payload_json 形如
// {"shots": [ShotSpec...], "outline": "..."}。
// 用 JSON 而不是 proto 的 repeated 字段，是为了让分镜表的**结构演进**
// 不必每次都重新生成两侧代码（分镜表仍在快速迭代期）。
func syncShotsFromPayload(job *domain.Job, payloadJSON string) error {
	var wrap struct {
		Shots   []json.RawMessage `json:"shots"`
		Outline string            `json:"outline"`
	}
	if err := json.Unmarshal([]byte(payloadJSON), &wrap); err != nil {
		return fmt.Errorf("worker: payload_json 不是合法 JSON: %w", err)
	}
	if len(wrap.Shots) == 0 {
		return nil
	}

	// 幂等：重复收到 plan 事件（例如断点续跑）时不应重复追加分镜。
	// 策略是「整体替换」而非「追加」—— 导演的产出是权威快照。
	// 但已经渲染过的镜头必须保留其状态，否则重跑会丢掉已完成的成果。
	existing := make(map[string]*domain.Shot, len(job.Shots))
	for _, s := range job.Shots {
		existing[s.ShotID] = s
	}

	shots := make([]*domain.Shot, 0, len(wrap.Shots))
	for i, raw := range wrap.Shots {
		// 复用 pb.ShotSpec 作为中间结构：它已经是两侧共用的契约，
		// 直接用它可以避免再维护一份纯 JSON 的结构体。
		var spec pb.ShotSpec
		if err := json.Unmarshal(raw, &spec); err != nil {
			return fmt.Errorf("worker: 第 %d 个分镜解析失败: %w", i, err)
		}
		shot := pbconv.ShotFromPB(&spec)
		if shot.ShotID == "" {
			// 导演可能不提供 shot_id，此时按索引稳定派生，保证 ID 可预测。
			shot.ShotID = domain.ShotID(job.JobID, i)
		}
		shot.JobID = job.JobID
		shot.Index = i
		// 标签必须合法，否则路由会失败。非法时降级为「氛围」镜头，
		// 让它至少能产出占位画面，而不是让整片卡住。
		if !shot.Tag.Valid() {
			shot.Tag = domain.TagAmbience
		}
		if engine, err := domain.EngineForTag(shot.Tag); err == nil {
			shot.Engine = engine
		}
		shot.Status = domain.StatusPending

		// 保留已有进度：只补齐叙事的元数据，不重置渲染结果。
		if old, ok := existing[shot.ShotID]; ok {
			shot.Status = old.Status
			shot.Attempt = old.Attempt
			shot.Artifact = old.Artifact
			shot.Feedbacks = old.Feedbacks
			shot.Error = old.Error
		}
		shots = append(shots, shot)
	}
	job.Shots = shots
	return nil
}

// ---------------------------------------------------------------------------
// 人类反馈闭环：单镜头重做
// ---------------------------------------------------------------------------

// HandleRenderShot 处理被打回镜头的重做。
//
// 与 HandleGenerateJob 的区别：这里走的是**细粒度 RPC**（ReviseShot），
// 只重跑「编码 -> 渲染 -> 审查」三个节点，跳过导演与全片合成。
// splicePartialRender 把局部重渲得到的片段拼回原片，返回拼接产物路径与方案。
//
// 三步，缺一不可：
//
//  1. **探测原片**拿到真实时长。拼接方案的切点完全由它决定，
//     用分镜表里的「计划时长」会让切点逐帧漂移。
//  2. **归一化新片段**到与原片完全一致的规格。渲染器产出的分辨率/帧率/
//     像素格式/色彩范围几乎必然与原片不同；规格不一致时 concat 会花屏
//     或直接报错。原片本来就是归一化流程的产物，新片段必须对齐它。
//  3. **规划并拼接**。若替换区间占比过大，PlanSplice 会判定拼接不划算，
//     此时直接把新片段当作整镜产物返回，而不是硬拼。
func (p *Processor) splicePartialRender(
	ctx context.Context,
	shot *domain.Shot,
	patchPath string,
	start, end float64,
	workDir string,
) (string, media.SplicePlan, error) {
	if patchPath == "" {
		return "", media.SplicePlan{}, fmt.Errorf("worker: 局部重渲染未返回片段路径")
	}
	basePath := shot.Artifact.VideoPath

	baseProbe, err := p.media.Probe(ctx, basePath)
	if err != nil {
		return "", media.SplicePlan{}, fmt.Errorf("worker: 探测原片失败: %w", err)
	}

	plan := media.PlanSplice(baseProbe.DurationSec, start, end)
	if plan.FullReplace {
		// 拼接不划算或区间非法：直接采用整段新产物。
		return patchPath, plan, nil
	}

	spec := p.media.DefaultNormalizeSpec()
	normPatch := filepath.Join(workDir, "patch_norm.mp4")
	if err := p.media.Normalize(ctx, patchPath, normPatch, spec); err != nil {
		return "", plan, fmt.Errorf("worker: 归一化局部重渲片段失败: %w", err)
	}

	out := filepath.Join(workDir, "spliced.mp4")
	if err := p.media.SpliceSegment(ctx, basePath, normPatch, out, plan); err != nil {
		return "", plan, fmt.Errorf("worker: 拼接局部重渲片段失败: %w", err)
	}
	return out, plan, nil
}

// HandleRenderShot 处理单镜头重做任务（人工反馈闭环的出口）。
func (p *Processor) HandleRenderShot(ctx context.Context, task RenderShotTask) error {
	jobID, shotID := task.Payload.JobID, task.Payload.ShotID
	ctx = logging.WithJob(ctx, jobID)
	lg := logging.FromContext(ctx).With(
		"job_id", jobID, "shot_id", shotID, "attempt", task.Payload.Attempt)

	job, err := p.store.GetJob(ctx, jobID)
	if err != nil {
		if errors.Is(err, store.ErrJobNotFound) {
			lg.Warn("任务不存在，忽略镜头重做消息")
			return nil
		}
		return err
	}
	shot := job.FindShot(shotID)
	if shot == nil {
		lg.Warn("分镜不存在，忽略镜头重做消息")
		return nil
	}

	// 幂等闸门：同一 (job, shot, attempt) 只处理一次。
	// Asynq 在「执行成功但确认失败」时会重复投递，没有这道闸门就会重复渲染（真金白银）。
	idemKey := fmt.Sprintf("%s:%s:%d", jobID, shotID, task.Payload.Attempt)
	ok, err := p.store.AcquireIdempotencyKey(ctx, idemKey, 30*time.Minute)
	if err != nil {
		return fmt.Errorf("worker: 幂等占位失败: %w", err)
	}
	if !ok {
		lg.Info("该次尝试已在处理中，跳过重复投递")
		return nil
	}

	// 只有真正失败（返回 error）时才释放占位，成功路径让 TTL 自然过期。
	success := false
	defer func() {
		if !success {
			_ = p.store.ReleaseIdempotencyKey(ctx, idemKey)
		}
	}()

	// 熔断判断：人工打回同样计入 attempt，超过上限就转人工而不是继续烧钱。
	// 人工额度是自动重试的 2 倍：人愿意多给几次机会，但也要有底线。
	if domain.MaxAttemptsExceeded(shot.Attempt, p.cfg.Pipeline.ShotMaxAttempts*2) {
		lg.Warn("人工打回次数已达上限，转人工处理")
		_, _ = p.store.UpdateJob(ctx, jobID, func(j *domain.Job) error {
			if s := j.FindShot(shotID); s != nil {
				s.Status = domain.StatusAwaitingHuman
				s.Error = "人工打回次数已达上限，需要人工直接编辑或接受"
			}
			return nil
		})
		_, _ = p.emit(ctx, &domain.Event{
			JobID: jobID, ShotID: shotID, Node: "hitl", Status: domain.StatusAwaitingHuman,
			Attempt: shot.Attempt, Message: "打回次数已达上限，转人工处理",
			Timestamp: time.Now().UTC(),
		})
		success = true
		return nil
	}

	// 把镜头推进到 GENERATING 并广播，让审核台立刻看到「正在重做」。
	if _, err := p.store.UpdateJob(ctx, jobID, func(j *domain.Job) error {
		s := j.FindShot(shotID)
		if s == nil {
			return nil
		}
		// 走一次状态机校验（非法迁移只告警不阻断：人工路径可能从任意状态进入）。
		if _, terr := domain.Transition(s.Status, domain.StatusGenerating); terr != nil {
			lg.Warn("人工重做的状态迁移未经登记，仍继续执行",
				"from", string(s.Status), "to", string(domain.StatusGenerating))
		}
		s.Status = domain.StatusGenerating
		s.UpdatedAt = time.Now().UTC()
		return nil
	}); err != nil {
		return err
	}
	_, _ = p.emit(ctx, &domain.Event{
		JobID: jobID, ShotID: shotID, Node: "code", Status: domain.StatusGenerating,
		Attempt: shot.Attempt, Message: "正在根据反馈重写代码…",
		Timestamp: time.Now().UTC(),
	})

	outputDir := p.shotWorkDir(jobID, shot.Index)

	// 局部重渲染：只在载荷指定了区间、且该镜头**已有可用产物**时才成立 ——
	// 拼接的前提是有一个可以保留前后段的原片。
	partial := task.Payload.WantsPartialRender() &&
		shot.Artifact != nil && shot.Artifact.VideoPath != ""

	req := &pb.ReviseShotRequest{
		JobId:          jobID,
		Shot:           pbconv.ShotToPB(shot),
		HumanComment:   task.Payload.HumanComment,
		Attempt:        int32(task.Payload.Attempt),
		StyleGuideJson: mustJSON(job.StyleGuide),
		OutputDir:      outputDir,
	}
	if partial {
		req.RangeStartSec = task.Payload.PatchStartSec
		req.RangeEndSec = task.Payload.PatchEndSec
		lg.Info("请求局部重渲染",
			"shot_id", shotID,
			"range_start_sec", task.Payload.PatchStartSec,
			"range_end_sec", task.Payload.PatchEndSec,
			"engine", string(shot.Engine))
	}

	resp, err := p.ai.ReviseShot(ctx, req)
	if err != nil {
		lg.Error("镜头重做调用失败", "error", err.Error())
		// 与主链路一致：未实现的能力不重试，直接归档。
		// 注意此时幂等占位尚未释放（success 仍为 false），defer 会把它清掉，
		// 保证人工之后真的补上实现时，重做请求仍能被处理。
		if errors.Is(err, ai.ErrNotImplemented) {
			lg.Warn("镜头重做能力尚未实现，跳过重试直接归档", "error", err.Error())
			return fmt.Errorf("%w: %w", asynq.SkipRetry, err)
		}
		return err
	}

	// 局部重渲的结果只是原片的一小段，必须拼回去才是完整镜头。
	// 服务端会如实告知是否真的只渲了该区间 —— 猜错的后果是把只渲了 2 秒的
	// 片段当成完整镜头拼进成片，那会毁掉整部片子的时长与音画同步。
	artifactPathOverride := ""
	if resp.GetPartialRangeHonored() && partial {
		spliced, plan, serr := p.splicePartialRender(ctx, shot, resp.GetArtifact().GetVideoPath(),
			task.Payload.PatchStartSec, task.Payload.PatchEndSec, outputDir)
		if serr != nil {
			// 拼接失败不能让本次重做白费：退回整片段替换，
			// 并留下明确的告警，避免「优化静默失效」。
			lg.Warn("局部重渲染拼接失败，退回整体替换该镜头",
				"error", serr.Error(), "shot_id", shotID)
			_, _ = p.emit(ctx, &domain.Event{
				JobID: jobID, ShotID: shotID, Node: "render",
				Message:   "局部重渲染拼接失败，已退回整体替换：" + serr.Error(),
				Error:     serr.Error(),
				Timestamp: time.Now().UTC(),
			})
		} else {
			artifactPathOverride = spliced
			lg.Info("局部重渲染拼接完成",
				"shot_id", shotID,
				"segments", plan.Segments,
				"saved_ratio", plan.SavedRatio())
			_, _ = p.emit(ctx, &domain.Event{
				JobID: jobID, ShotID: shotID, Node: "render",
				Message: fmt.Sprintf("已只重渲 %.1fs~%.1fs 并拼接回原片（省下约 %.0f%% 的渲染量）",
					task.Payload.PatchStartSec, task.Payload.PatchEndSec, plan.SavedRatio()*100),
				Payload: map[string]any{
					"partial_render": true,
					"segments":       plan.Segments,
					"saved_ratio":    plan.SavedRatio(),
				},
				Timestamp: time.Now().UTC(),
			})
		}
	}

	// 把重做结果写回状态。
	final, err := p.store.UpdateJob(ctx, jobID, func(j *domain.Job) error {
		s := j.FindShot(shotID)
		if s == nil {
			return nil
		}
		if resp.GetShot() != nil {
			s.Code = resp.GetShot().GetCode()
			s.Language = resp.GetShot().GetLanguage()
		}
		if art := pbconv.ArtifactFromPB(resp.GetArtifact()); art != nil {
			if artifactPathOverride != "" {
				// 拼接产物才是完整的镜头；直接覆盖路径，其余元数据沿用服务端回填的。
				art.VideoPath = artifactPathOverride
			}
			s.Artifact = art
		}
		if fb := resp.GetFeedback(); fb != nil {
			s.Feedbacks = append(s.Feedbacks, pbconv.FeedbackFromPB(fb))
			if fb.GetPassed() {
				s.Status = domain.StatusApproved
			} else {
				// 重做后仍未通过自动审查 -> 交给人做最终判断，而不是再自动烧一轮。
				s.Status = domain.StatusAwaitingHuman
				s.Error = "重做后仍未通过自动审查，请人工确认"
			}
		} else if resp.GetSuccess() {
			s.Status = domain.StatusApproved
		} else {
			s.Status = domain.StatusFailed
			s.Error = resp.GetError()
		}
		s.UpdatedAt = time.Now().UTC()

		if st := j.Stat(); st.Approved == st.Total && st.Total > 0 {
			j.Status = domain.JobComposing
		}
		return nil
	})
	if err != nil {
		return err
	}

	// 安全取值：重做过程可能因为并发的人工操作导致镜头被替换，
	// 这里必须判空，否则一次正常竞态会升级成 panic。
	finalStatus := domain.StatusFailed
	if s := final.FindShot(shotID); s != nil {
		finalStatus = s.Status
	}
	_, _ = p.emit(ctx, &domain.Event{
		JobID: jobID, ShotID: shotID, Node: "critique",
		Status:    finalStatus,
		Attempt:   task.Payload.Attempt,
		Message:   "镜头重做完成",
		Progress:  final.ProgressRatio(),
		Timestamp: time.Now().UTC(),
	})

	if final.Status == domain.JobComposing {
		if _, err := p.q.EnqueueComposeJob(ctx, &queue.ComposeJobPayload{
			JobID: jobID, EnqueuedAt: time.Now().UTC(),
		}); err != nil {
			lg.Error("合成任务入队失败", "error", err.Error())
		}
	}
	success = true
	return nil
}

// ---------------------------------------------------------------------------
// 内部工具
// ---------------------------------------------------------------------------

// transitionJob 更新任务级状态并追加一条事件。
func (p *Processor) transitionJob(
	ctx context.Context, jobID string, status domain.JobStatus,
	node string, attempt int, message string, payload map[string]any,
) error {
	job, err := p.store.UpdateJob(ctx, jobID, func(j *domain.Job) error {
		j.Status = status
		j.UpdatedAt = time.Now().UTC()
		return nil
	})
	if err != nil {
		return err
	}
	_, err = p.emit(ctx, &domain.Event{
		JobID: jobID, Node: node, Message: message,
		Attempt: attempt, Progress: job.ProgressRatio(), Payload: payload,
		Timestamp: time.Now().UTC(),
	})
	return err
}

// emit 把事件持久化到 Redis 事件流。
//
// 「先落地、后广播」的顺序是刻意的：AppendEvent 内部会把事件 Publish 到
// scid:job:<id>:stream 频道，api 进程订阅后再推给 WebSocket 客户端。
// 这样任何客户端收到的事件，都一定能从历史接口重新读到 ——
// 否则断线重连会出现事件缺口（前端表现为进度回跳）。
func (p *Processor) emit(ctx context.Context, ev *domain.Event) (*domain.Event, error) {
	stored, err := p.store.AppendEvent(ctx, ev)
	if err != nil {
		logging.FromContext(ctx).Error("写入事件流失败", "error", err.Error(), "job_id", ev.JobID)
		return nil, err
	}
	return stored, nil
}

// shotWorkDir 返回某个镜头的工作目录。
//
// 约定：所有中间产物集中在 <WorkDir>/<jobID>/shot_<index>/ 下，
// 既方便按任务整体清理，也让 Python 侧（通过 output_dir 参数）与 Go 侧看到同一路径。
func (p *Processor) shotWorkDir(jobID string, shotIndex int) string {
	return filepath.Join(p.cfg.Media.WorkDir, jobID, fmt.Sprintf("shot_%03d", shotIndex))
}

// jobWorkDir 返回任务级工作目录（合成产物放这里）。
func (p *Processor) jobWorkDir(jobID string) string {
	return filepath.Join(p.cfg.Media.WorkDir, jobID)
}

// mustJSON 序列化为 JSON 字符串；失败返回 "{}"。
// 仅用于「可选的辅助字段」（如风格约束），失败不应阻断主流程。
func mustJSON(v any) string {
	if v == nil {
		return "{}"
	}
	buf, err := json.Marshal(v)
	if err != nil {
		return "{}"
	}
	return string(buf)
}
