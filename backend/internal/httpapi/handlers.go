package httpapi

import (
	"context"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/itJinYu/SciDirector/backend/internal/domain"
	"github.com/itJinYu/SciDirector/backend/internal/logging"
	"github.com/itJinYu/SciDirector/backend/internal/queue"
)

// Server 持有 HTTP handler 所需的依赖。
type Server struct {
	deps Deps
	// subs 管理每个任务的 Redis 事件订阅（worker 与 api 可能在不同进程，
	// 因此事件统一经由 Redis 广播而非进程内 Hub）。
	subs *jobSubscriptions
}

// NewServer 构造 HTTP 服务。
func NewServer(deps Deps) *Server {
	return &Server{
		deps: deps,
		subs: newJobSubscriptions(deps.Store, deps.Hub),
	}
}

// ---------------------------------------------------------------------------
// 健康检查
// ---------------------------------------------------------------------------

// HandleHealthz 是**存活**探针：只要进程还能响应就返回 200。
//
// 刻意不检查下游依赖：如果 Redis 挂了就把 api 也重启，只会让故障雪上加霜。
// 依赖检查属于 readiness（/readyz）。
func (s *Server) HandleHealthz(c *gin.Context) {
	respondOK(c, HealthResponse{
		Status:    "ok",
		Version:   s.deps.Version,
		UptimeSec: time.Since(s.deps.StartedAt).Seconds(),
	})
}

// HandleReadyz 是**就绪**探针：检查 Redis 与 Python 大脑。
// 任一不可用时返回 503，让编排系统把流量摘除。
func (s *Server) HandleReadyz(c *gin.Context) {
	ctx := c.Request.Context()
	components := map[string]string{}

	status := "ok"
	httpStatus := http.StatusOK

	// Redis 是硬依赖：没有它连任务状态都读不到。
	if err := s.deps.Store.Ping(ctx); err != nil {
		components["redis"] = "down: " + err.Error()
		status = "down"
		httpStatus = http.StatusServiceUnavailable
	} else {
		components["redis"] = "ok"
	}

	// AI 大脑不可用只降级不致命：已提交的任务仍可查询状态，
	// 新任务会被拒绝（见 HandleGenerate 的显式检查）。
	aiCtx, cancel := withTimeout(ctx, 3*time.Second)
	defer cancel()
	if _, err := s.deps.AI.Health(aiCtx); err != nil {
		components["ai"] = "degraded: " + err.Error()
		if status == "ok" {
			status = "degraded"
		}
	} else {
		components["ai"] = "ok"
	}

	resp := HealthResponse{
		Status:     status,
		Version:    s.deps.Version,
		UptimeSec:  time.Since(s.deps.StartedAt).Seconds(),
		Components: components,
	}
	if httpStatus != http.StatusOK {
		c.JSON(httpStatus, gin.H{"ok": false, "data": resp})
		return
	}
	respondOK(c, resp)
}

// HandleVersion 返回构建信息，便于确认线上跑的是哪一版。
func (s *Server) HandleVersion(c *gin.Context) {
	respondOK(c, gin.H{
		"version":    s.deps.Version,
		"env":        s.deps.Config.Env,
		"started_at": s.deps.StartedAt,
		"uptime_sec": time.Since(s.deps.StartedAt).Seconds(),
	})
}

// ---------------------------------------------------------------------------
// 任务
// ---------------------------------------------------------------------------

// HandleGenerate 接收脚本并投递生成任务。
//
// 这是一个**快速返回**的接口：真正的生成需要数分钟到数十分钟，
// 因此这里只做「建任务 + 入队」，结果通过 WebSocket 与查询接口获取。
// 同步等待生成完成会让 HTTP 连接超时，也会让前端无法展示中间进度。
func (s *Server) HandleGenerate(c *gin.Context) {
	var req GenerateRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, "请求参数非法", err)
		return
	}

	ctx := c.Request.Context()
	lg := logging.FromContext(ctx)

	// 快速失败：AI 大脑不可用时立刻拒绝，避免任务入队后长时间卡在队列里，
	// 也让用户马上得到「稍后重试」而不是「一直转圈」。
	healthCtx, cancel := withTimeout(ctx, 3*time.Second)
	defer cancel()
	if _, err := s.deps.AI.Health(healthCtx); err != nil {
		abortWith(c, http.StatusServiceUnavailable, ErrCodeUpstream,
			"AI 服务当前不可用，无法接受新的生成任务", err)
		return
	}

	jobID := NewJobID()
	// 目标时长的默认值：科普短视频的常见长度。
	target := req.TargetDurationSec
	if target <= 0 {
		target = 90
	}
	locale := req.Locale
	if locale == "" {
		locale = "zh-CN"
	}
	now := time.Now().UTC()

	job := &domain.Job{
		JobID:             jobID,
		RawScript:         req.RawScript,
		StyleGuide:        req.StyleGuide,
		TargetDurationSec: target,
		Locale:            locale,
		Status:            domain.JobCreated,
		Shots:             []*domain.Shot{}, // 分镜由导演智能体在 worker 中填充
		CreatedAt:         now,
		UpdatedAt:         now,
	}
	if err := s.deps.Store.SaveJob(ctx, job); err != nil {
		mapError(c, err)
		return
	}

	// 记录首条事件，使 WS 的「加入即重放」从任务创建那一刻就有内容。
	if _, err := s.deps.Store.AppendEvent(ctx, &domain.Event{
		JobID:     jobID,
		Node:      "api",
		Message:   "任务已创建，等待导演智能体拆解脚本",
		Progress:  0,
		Timestamp: now,
	}); err != nil {
		// 事件写入失败不阻塞主流程：任务本身已经持久化，前端仍可查询。
		lg.Warn("写入创建事件失败", "error", err.Error())
	}

	taskID, err := s.deps.Queue.EnqueueGenerateJob(ctx, &queue.GenerateJobPayload{
		JobID:             jobID,
		RawScript:         req.RawScript,
		TargetDurationSec: target,
		Locale:            locale,
		EnqueuedAt:        now,
	})
	if err != nil {
		// 入队失败说明队列不可用，任务永远跑不起来：标记任务失败并返回 503，
		// 而不是留下一个永远停在 CREATED 的僵尸任务。
		_, _ = s.deps.Store.UpdateJob(ctx, jobID, func(j *domain.Job) error {
			j.Status = domain.JobFailed
			j.Error = "任务入队失败：" + err.Error()
			return nil
		})
		mapError(c, err)
		return
	}

	lg.Info("生成任务已受理",
		"job_id", jobID,
		"task_id", taskID,
		"target_duration_sec", target,
		"script_bytes", len(req.RawScript),
	)

	respondAccepted(c, GenerateResponse{
		JobID:     jobID,
		Status:    job.Status,
		TaskID:    taskID,
		CreatedAt: now,
		WSURL:     "/ws/jobs/" + jobID,
	})
}

// HandleGetJob 返回任务详情（含分镜表）。
func (s *Server) HandleGetJob(c *gin.Context) {
	jobID := c.Param("jobID")
	job, err := s.deps.Store.GetJob(c.Request.Context(), jobID)
	if err != nil {
		mapError(c, err)
		return
	}
	respondOK(c, JobResponse{
		Job: job, Stat: job.Stat(), Progress: job.ProgressRatio(), Cost: job.CostSnapshot(),
	})
}

// HandleGetJobCost 返回任务的成本快照。
//
// 单独开一个端点而不是只在任务详情里带：成本是**审计与容量规划**的视角，
// 它要能被单独查询、单独采集（例如定时把开销异常的任务捞出来），
// 而不必每次都把整份分镜表拉下来。
func (s *Server) HandleGetJobCost(c *gin.Context) {
	job, err := s.deps.Store.GetJob(c.Request.Context(), c.Param("jobID"))
	if err != nil {
		mapError(c, err)
		return
	}
	respondOK(c, CostResponse{JobID: job.JobID, Cost: job.CostSnapshot()})
}

// HandleListShots 只返回分镜数组。
func (s *Server) HandleListShots(c *gin.Context) {
	job, err := s.deps.Store.GetJob(c.Request.Context(), c.Param("jobID"))
	if err != nil {
		mapError(c, err)
		return
	}
	respondOK(c, ShotsResponse{JobID: job.JobID, Total: len(job.Shots), Shots: job.Shots})
}

// HandleListEvents 支持增量拉取事件流（WS 的降级通道 / 断线补齐）。
func (s *Server) HandleListEvents(c *gin.Context) {
	jobID := c.Param("jobID")
	afterID, _ := strconv.ParseInt(c.DefaultQuery("after_id", "0"), 10, 64)

	// 先确认任务存在，避免对不存在的 job 返回空数组造成误解。
	if _, err := s.deps.Store.GetJob(c.Request.Context(), jobID); err != nil {
		mapError(c, err)
		return
	}
	events, err := s.deps.Store.ListEvents(c.Request.Context(), jobID, afterID)
	if err != nil {
		mapError(c, err)
		return
	}
	respondOK(c, EventsResponse{JobID: jobID, AfterID: afterID, Events: events})
}

// ---------------------------------------------------------------------------
// 人类反馈闭环（HITL）
// ---------------------------------------------------------------------------

// HandleRejectShot 接收审核员的打回意见，并把该镜头重新入队。
//
// 关键设计：只重做**这一个镜头**，不重跑全片。
// 一次全片生成可能耗费数十次渲染与 VLM 调用；按镜头重做能把返工成本降到 1/N。
func (s *Server) HandleRejectShot(c *gin.Context) {
	var req RejectRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest,
			"打回意见不能为空且需具备可执行性（2~2000 字）", err)
		return
	}
	jobID, shotID := c.Param("jobID"), c.Param("shotID")
	ctx := c.Request.Context()

	// 用一个原子更新完成「校验 + 状态迁移 + 记录意见」，避免中间态被其他请求观察到。
	var (
		newAttempt int
		newStatus  domain.ShotStatus
	)
	job, err := s.deps.Store.UpdateJob(ctx, jobID, func(j *domain.Job) error {
		shot := j.FindShot(shotID)
		if shot == nil {
			return errShotNotFound
		}
		// 已通过的镜头也允许重做（人工后来发现问题的场景）：
		// 状态机已显式允许 APPROVED -> RETRYING。
		next, err := domain.Transition(shot.Status, domain.StatusRetrying)
		if err != nil {
			return err
		}
		shot.Status = next
		shot.Attempt++ // 人工打回同样消耗一次尝试额度，避免无限返工
		shot.Feedbacks = append(shot.Feedbacks, domain.Feedback{
			Passed:      false,
			Source:      domain.FeedbackHuman,
			Attempt:     shot.Attempt,
			Issues:      []string{req.Comment},
			Suggestions: []string{req.Comment},
			CreatedAt:   time.Now().UTC(),
		})
		shot.UpdatedAt = time.Now().UTC()
		newAttempt = shot.Attempt
		newStatus = shot.Status
		// 打回后任务整体回到 RENDERING 状态（可能之前是 COMPLETED/PARTIAL）。
		j.Status = domain.JobRendering
		return nil
	})
	if err != nil {
		if errors.Is(err, errShotNotFound) {
			abortWith(c, http.StatusNotFound, ErrCodeNotFound, "分镜不存在", err)
			return
		}
		mapError(c, err)
		return
	}

	// 事件交由 Redis 事件流广播（AppendEvent 内含 Publish），
	// 由 api 侧的任务订阅统一扇出给 WS 客户端 —— 不在此处直接操作 Hub，
	// 否则单进程部署下会与订阅路径重复推送同一条事件。
	if _, err := s.deps.Store.AppendEvent(ctx, &domain.Event{
		JobID:     jobID,
		ShotID:    shotID,
		Node:      "hitl",
		Status:    newStatus,
		Attempt:   newAttempt,
		Message:   "审核员打回该镜头：" + req.Comment,
		Progress:  job.ProgressRatio(),
		Timestamp: time.Now().UTC(),
	}); err != nil {
		logging.FromContext(ctx).Warn("写入打回事件失败", "shot_id", shotID, "error", err.Error())
	}

	taskID, err := s.deps.Queue.EnqueueRenderShot(ctx, &queue.RenderShotPayload{
		JobID:        jobID,
		ShotID:       shotID,
		Attempt:      newAttempt,
		HumanComment: req.Comment,
		TriggeredBy:  "api",
		EnqueuedAt:   time.Now().UTC(),
	})
	if err != nil {
		mapError(c, err)
		return
	}

	respondAccepted(c, RejectResponse{
		JobID: jobID, ShotID: shotID, Status: newStatus, Attempt: newAttempt, TaskID: taskID,
	})
}

// HandleApproveShot 允许审核员在镜头处于 AWAITING_HUMAN 时直接放行。
//
// 这是熔断后的必要出口：自动化重试 N 次仍不合格时，人工必须能
// 「接受当前效果并继续」，否则整条片子永远无法交付。
func (s *Server) HandleApproveShot(c *gin.Context) {
	jobID, shotID := c.Param("jobID"), c.Param("shotID")
	ctx := c.Request.Context()

	job, err := s.deps.Store.UpdateJob(ctx, jobID, func(j *domain.Job) error {
		shot := j.FindShot(shotID)
		if shot == nil {
			return errShotNotFound
		}
		next, err := domain.Transition(shot.Status, domain.StatusApproved)
		if err != nil {
			return err
		}
		shot.Status = next
		shot.UpdatedAt = time.Now().UTC()
		// 若所有镜头都已通过，任务可以直接进入合成阶段（由 worker 兜底推进）。
		if st := j.Stat(); st.Approved == st.Total && st.Total > 0 {
			j.Status = domain.JobComposing
		}
		return nil
	})
	if err != nil {
		if errors.Is(err, errShotNotFound) {
			abortWith(c, http.StatusNotFound, ErrCodeNotFound, "分镜不存在", err)
			return
		}
		mapError(c, err)
		return
	}

	if _, err := s.deps.Store.AppendEvent(ctx, &domain.Event{
		JobID:     jobID,
		ShotID:    shotID,
		Node:      "hitl",
		Status:    domain.StatusApproved,
		Message:   "审核员手动通过该镜头",
		Progress:  job.ProgressRatio(),
		Timestamp: time.Now().UTC(),
	}); err != nil {
		logging.FromContext(ctx).Warn("写入通过事件失败", "shot_id", shotID, "error", err.Error())
	}

	// 放行之后必须**主动把合成任务入队**。
	//
	// 只把 Job.Status 改成 COMPOSING 是不够的：状态本身不会驱动任何东西，
	// 必须有人把任务放进队列。worker 只在「处理单镜头重做」的收尾处会
	// 顺带入队合成，而人工放行是走 API 的 —— 没有这一步，最后一个熔断镜头
	// 被放行后任务会**永远停在 COMPOSING**，前端看着进度 100% 却永远等不到成片。
	// 这是熔断出口（C5）能真正闭环的最后一环。
	if job.Status == domain.JobComposing {
		taskID, qerr := s.deps.Queue.EnqueueComposeJob(ctx, &queue.ComposeJobPayload{
			JobID: jobID, EnqueuedAt: time.Now().UTC(),
		})
		if qerr != nil {
			mapError(c, qerr)
			return
		}
		logging.FromContext(ctx).Info("所有镜头已通过，合成任务已入队",
			"job_id", jobID, "task_id", taskID)
		if _, err := s.deps.Store.AppendEvent(ctx, &domain.Event{
			JobID: jobID, Node: "compose",
			Message:   "所有镜头均已通过，开始合成成片",
			Progress:  job.ProgressRatio(),
			Timestamp: time.Now().UTC(),
		}); err != nil {
			logging.FromContext(ctx).Warn("写入合成事件失败", "error", err.Error())
		}
	}

	respondOK(c, ApproveResponse{
		JobID: jobID, ShotID: shotID, Status: domain.StatusApproved,
		ComposeEnqueued: job.Status == domain.JobComposing,
	})
}

// HandlePatchShot 修改分镜的文案字段，并可选择立即重做。
//
// 为什么需要它：审核员打回时常常发现**问题出在文案而不是画面** ——
// 「画外音说'三种情况'但画面只画了两种」。此时重写整个镜头是浪费：
// 改掉 narration 再重渲就够了，而导演智能体的原始意图得以保留。
//
// 与打回接口的分工：
//   - 打回（reject）：画面不对，把意见回灌给编码智能体重写代码；
//   - 编辑（patch）：**文案不对**，直接改掉分镜字段再重渲。
//
// 两者都会消耗一次 attempt 额度（当 Redo=true 时），口径一致。
func (s *Server) HandlePatchShot(c *gin.Context) {
	var req PatchShotRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest,
			"分镜编辑请求非法（字段长度上限 2000 字）", err)
		return
	}
	if req.Narration == nil && req.VisualBrief == nil && !req.Redo {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest,
			"没有需要变更的内容：请至少修改一个字段或指定 redo", nil)
		return
	}

	jobID, shotID := c.Param("jobID"), c.Param("shotID")
	ctx := c.Request.Context()

	var (
		changed    []string
		newAttempt int
		newStatus  domain.ShotStatus
	)
	job, err := s.deps.Store.UpdateJob(ctx, jobID, func(j *domain.Job) error {
		shot := j.FindShot(shotID)
		if shot == nil {
			return errShotNotFound
		}

		// 文案字段：只有显式给出（非 nil）才改，因此「清空」也是一个合法操作。
		if req.Narration != nil {
			shot.Narration = strings.TrimSpace(*req.Narration)
			changed = append(changed, "narration")
		}
		if req.VisualBrief != nil {
			shot.VisualBrief = strings.TrimSpace(*req.VisualBrief)
			changed = append(changed, "visual_brief")
		}

		if req.Redo {
			next, terr := domain.Transition(shot.Status, domain.StatusRetrying)
			if terr != nil {
				return terr
			}
			shot.Status = next
			shot.Attempt++ // 与打回一致：编辑后重做同样消耗一次额度
			if req.Comment != "" {
				shot.Feedbacks = append(shot.Feedbacks, domain.Feedback{
					Passed:      false,
					Source:      domain.FeedbackHuman,
					Attempt:     shot.Attempt,
					Issues:      []string{req.Comment},
					Suggestions: []string{req.Comment},
					CreatedAt:   time.Now().UTC(),
				})
			}
			newAttempt = shot.Attempt
			newStatus = shot.Status
			j.Status = domain.JobRendering
		}

		shot.UpdatedAt = time.Now().UTC()
		return nil
	})
	if err != nil {
		if errors.Is(err, errShotNotFound) {
			abortWith(c, http.StatusNotFound, ErrCodeNotFound, "分镜不存在", err)
			return
		}
		mapError(c, err)
		return
	}

	msg := "审核员修改了分镜文案"
	if len(changed) > 0 {
		msg += "（" + strings.Join(changed, "、") + "）"
	}
	if _, err := s.deps.Store.AppendEvent(ctx, &domain.Event{
		JobID: jobID, ShotID: shotID, Node: "hitl",
		Status:    newStatus,
		Attempt:   newAttempt,
		Message:   msg,
		Progress:  job.ProgressRatio(),
		Timestamp: time.Now().UTC(),
	}); err != nil {
		logging.FromContext(ctx).Warn("写入分镜编辑事件失败", "error", err.Error())
	}

	resp := PatchShotResponse{
		JobID: jobID, ShotID: shotID, Status: domain.StatusApproved,
		Attempt: newAttempt, Changed: changed,
	}
	if s2 := job.FindShot(shotID); s2 != nil {
		resp.Status = s2.Status
	}

	if req.Redo {
		taskID, qerr := s.deps.Queue.EnqueueRenderShot(ctx, &queue.RenderShotPayload{
			JobID:        jobID,
			ShotID:       shotID,
			Attempt:      newAttempt,
			HumanComment: req.Comment,
			TriggeredBy:  "api",
			EnqueuedAt:   time.Now().UTC(),
		})
		if qerr != nil {
			mapError(c, qerr)
			return
		}
		resp.TaskID = taskID
	}

	respondOK(c, resp)
}

// errShotNotFound 是 handler 内部哨兵，用于把「分镜不存在」与仓储错误区分开，
// 从而返回 404 而不是 500。
var errShotNotFound = errors.New("httpapi: 分镜不存在")

// withTimeout 统一封装 context.WithTimeout，避免调用点忘记 defer cancel 造成
// goroutine 与定时器泄漏（这是 Go 服务里最常见的一类长跑内存增长原因）。
func withTimeout(parent context.Context, d time.Duration) (context.Context, context.CancelFunc) {
	return context.WithTimeout(parent, d)
}
