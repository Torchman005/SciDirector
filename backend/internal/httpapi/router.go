package httpapi

import (
	"context"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/itJinYu/SciDirector/backend/internal/domain"
	"github.com/itJinYu/SciDirector/backend/internal/logging"
	"github.com/itJinYu/SciDirector/backend/internal/queue"
	"github.com/itJinYu/SciDirector/backend/internal/ws"
)

// NewRouter 组装完整的 HTTP 路由。
//
// 路由分组约定：/api/v1 下的都是业务接口，/ws 下是长连接，
// /healthz 与 /readyz 不带版本号（编排系统依赖它们，不应随 API 版本变化）。
func NewRouter(s *Server, deps Deps) *gin.Engine {
	if deps.Config.IsDev() {
		gin.SetMode(gin.DebugMode)
	} else {
		gin.SetMode(gin.ReleaseMode)
	}

	// 不使用 gin.Default()：它内置的 Logger/Recovery 无法与我们的结构化日志
	// 和 trace_id 打通，因此全部中间件显式挂载。
	r := gin.New()
	r.RedirectTrailingSlash = true

	r.Use(
		TraceMiddleware(),
		RecoveryMiddleware(),
		AccessLogMiddleware(),
		CORSMiddleware(deps.Config.HTTP.CORSAllowedOrigins),
	)
	// 限制请求体大小：脚本类接口不应接受超大 body，避免内存放大攻击。
	r.MaxMultipartMemory = 8 << 20

	// --- 编排系统探针（无版本前缀）---
	r.GET("/healthz", s.HandleHealthz)
	r.GET("/readyz", s.HandleReadyz)
	r.GET("/version", s.HandleVersion)

	v1 := r.Group("/api/v1")
	{
		v1.POST("/generate", s.HandleGenerate)

		// 队列可观测：让「任务卡住了吗」有一个不需要翻 Redis 的答案。
		// 放在 /api/v1 下而不是 /healthz：它是运维视角的诊断信息，
		// 不参与编排系统的存活/就绪判定 —— 队列积压不该让实例被摘除。
		v1.GET("/queue/stats", s.HandleQueueStats)

		jobs := v1.Group("/jobs/:jobID")
		{
			jobs.GET("", s.HandleGetJob)
			jobs.GET("/shots", s.HandleListShots)
			jobs.GET("/events", s.HandleListEvents)
			jobs.POST("/shots/:shotID/reject", s.HandleRejectShot)
			jobs.POST("/shots/:shotID/approve", s.HandleApproveShot)
			// PATCH 而不是 PUT：只改给出的字段，
			// 「未给出」与「给出空串」是两种不同意图（见 PatchShotRequest 的说明）。
			jobs.PATCH("/shots/:shotID", s.HandlePatchShot)
		}
	}

	// --- 人类反馈闭环：WebSocket ---
	r.GET("/ws/jobs/:jobID", s.HandleJobWS)

	// 未匹配路由统一返回结构化 404，避免 gin 的纯文本响应破坏前端统一处理。
	r.NoRoute(func(c *gin.Context) {
		abortWith(c, http.StatusNotFound, ErrCodeNotFound, "接口不存在", nil)
	})

	return r
}

// HandleQueueStats 返回队列深度与失败情况。
//
// 这些指标解决的具体问题：
//   - size/retry 持续不为 0 → 有任务在反复失败；
//   - archived → 任务级最终失败，需要人工介入；
//   - latency_sec → 用户要等多久才开始执行。它比深度更能说明体感：
//     深度 3 但都等了 10 分钟，与深度 300 但只等 1 秒，是完全不同的两种问题。
//
// 采集失败返回 503 而不是 500：这是依赖不可用（Redis 抖动），
// 而不是服务端代码错误，语义上更准确，也便于告警分流。
func (s *Server) HandleQueueStats(c *gin.Context) {
	if s.deps.Inspector == nil {
		abortWith(c, http.StatusNotImplemented, ErrCodeInternal,
			"队列观测未启用（未配置 Inspector）", nil)
		return
	}

	stats, err := s.deps.Inspector.Stats(c.Request.Context())
	if err != nil {
		abortWith(c, http.StatusServiceUnavailable, ErrCodeInternal, "队列状态采集失败", err)
		return
	}
	c.JSON(http.StatusOK, stats)
}

// HandleJobWS 处理审核台的 WebSocket 连接。
//
// 生命周期：
//  1. 升级协议；
//  2. 立即推送一份「任务快照 + 历史事件」，使刷新页面不丢上下文；
//  3. 之后由 api 侧的任务订阅（Redis Pub/Sub）持续推送增量事件；
//  4. 同时接收上行的人工反馈消息。
//
// 注意：HTTP 层的写超时对 WS 不适用（连接会长期存活），
// 因此该路由依赖 cmd/api 中 WriteTimeout=0 的配置。
func (s *Server) HandleJobWS(c *gin.Context) {
	jobID := c.Param("jobID")
	ctx := c.Request.Context()

	// 连接前先确认任务存在：否则前端会连上一个永远没有事件的空频道，
	// 表现为「一直在转圈」，很难排查。
	if _, err := s.deps.Store.GetJob(ctx, jobID); err != nil {
		c.JSON(http.StatusNotFound, APIError{Code: ErrCodeNotFound, Message: "任务不存在"})
		return
	}

	// onConnect：把快照与历史事件一次性下发。
	// 这样前端只有一条「首包」路径要处理，不必区分「首次连接」与「重连」。
	onConnect := func(cctx context.Context) *ws.Message {
		job, err := s.deps.Store.GetJob(cctx, jobID)
		if err != nil {
			return &ws.Message{Type: "error", Data: "读取任务失败: " + err.Error()}
		}
		events, err := s.deps.Store.ListEvents(cctx, jobID, 0)
		if err != nil {
			events = nil
		}
		var lastSeq int64
		if n := len(events); n > 0 {
			lastSeq = events[n-1].EventID
		}
		return &ws.Message{
			Type: "snapshot",
			Seq:  lastSeq,
			Data: gin.H{
				"job":      job,
				"stat":     job.Stat(),
				"progress": job.ProgressRatio(),
				"events":   events,
			},
		}
	}

	// 建立/复用到 Redis 的任务事件订阅。必须在 Serve 之前完成：
	// 否则「升级成功」到「开始订阅」之间产生的事件只能靠历史重放补齐，
	// 前端会表现为短时间内少一条实时推送。
	release := s.subs.acquire(jobID)
	defer release()

	s.deps.Hub.Serve(c.Writer, c.Request, jobID, onConnect, s.handleInbound)
}

// handleInbound 处理来自前端的上行消息。
//
// 支持三类：
//   - "feedback"：人工对某镜头的意见（等价于 REST 的打回接口，走 WS 是为了低延迟交互）；
//   - "resync"  ：前端重连后请求补发 after_id 之后的事件；
//   - "ping"    ：应用层心跳（某些代理会吞掉 WS 控制帧，应用层 ping 更可靠）。
func (s *Server) handleInbound(ctx context.Context, jobID string, msg ws.InboundMessage) *ws.Message {
	lg := logging.FromContext(ctx).With("job_id", jobID)

	switch msg.Type {
	case "resync":
		events, err := s.deps.Store.ListEvents(ctx, jobID, msg.AfterID)
		if err != nil {
			return &ws.Message{Type: "error", Data: "补发事件失败: " + err.Error()}
		}
		var lastSeq int64
		if n := len(events); n > 0 {
			lastSeq = events[n-1].EventID
		}
		return &ws.Message{Type: "events", Seq: lastSeq, Data: events}

	case "feedback":
		if msg.ShotID == "" || msg.Comment == "" {
			return &ws.Message{Type: "error", Data: "feedback 消息需要 shot_id 与 comment"}
		}
		if err := s.applyHumanFeedback(ctx, jobID, msg.ShotID, msg.Comment); err != nil {
			lg.Warn("处理 WS 人工反馈失败", "shot_id", msg.ShotID, "error", err.Error())
			return &ws.Message{Type: "error", Data: err.Error()}
		}
		return &ws.Message{Type: "ack", Data: gin.H{"shot_id": msg.ShotID, "accepted": true}}

	case "ping":
		return &ws.Message{Type: "pong", Data: time.Now().UTC()}

	default:
		return &ws.Message{Type: "error", Data: "未知消息类型: " + msg.Type}
	}
}

// applyHumanFeedback 是 WS 路径下的人工打回实现。
// 与 REST 版本共享同一套状态迁移规则，只是入口不同（低延迟交互 vs 表单提交）。
func (s *Server) applyHumanFeedback(ctx context.Context, jobID, shotID, comment string) error {
	job, err := s.deps.Store.UpdateJob(ctx, jobID, func(j *domain.Job) error {
		shot := j.FindShot(shotID)
		if shot == nil {
			return errShotNotFound
		}
		next, err := domain.Transition(shot.Status, domain.StatusRetrying)
		if err != nil {
			return err
		}
		shot.Status = next
		// 人工打回同样消耗一次尝试额度：这是防止「无限返工」的成本闸门。
		shot.Attempt++
		shot.Feedbacks = append(shot.Feedbacks, domain.Feedback{
			Passed:      false,
			Source:      domain.FeedbackHuman,
			Attempt:     shot.Attempt,
			Issues:      []string{comment},
			Suggestions: []string{comment},
			CreatedAt:   time.Now().UTC(),
		})
		shot.UpdatedAt = time.Now().UTC()
		j.Status = domain.JobRendering
		return nil
	})
	if err != nil {
		return err
	}

	shot := job.FindShot(shotID)
	attempt := 0
	if shot != nil {
		attempt = shot.Attempt
	}

	// 先持久化再（隐式）广播：AppendEvent 内部会 Publish 到任务频道，
	// 由本进程的订阅转发给 WS 客户端。广播出去的事件必须已经能从历史接口读到，
	// 否则前端重连时会「凭空少一条」，表现为进度回跳。
	ev := domain.Event{
		JobID: jobID, ShotID: shotID, Node: "hitl", Status: domain.StatusRetrying,
		Attempt: attempt, Message: "审核员打回该镜头：" + comment,
		Progress: job.ProgressRatio(), Timestamp: time.Now().UTC(),
	}
	if _, err := s.deps.Store.AppendEvent(ctx, &ev); err != nil {
		logging.FromContext(ctx).Warn("写入打回事件失败", "shot_id", shotID, "error", err.Error())
	}

	_, err = s.deps.Queue.EnqueueRenderShot(ctx, &queue.RenderShotPayload{
		JobID: jobID, ShotID: shotID, Attempt: attempt,
		HumanComment: comment, TriggeredBy: "api", EnqueuedAt: time.Now().UTC(),
	})
	return err
}
