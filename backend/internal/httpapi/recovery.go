package httpapi

import (
	"context"
	"errors"
	"strings"
	"time"

	"github.com/itJinYu/SciDirector/backend/internal/domain"
)

const automaticRepairComment = "自动诊断并修复：根据系统错误、源码及审核记录定位问题，修复后重新渲染并审核。"

var errRepairBusy = errors.New("镜头正在处理中，请等待本次修复结束")

func repairComment(comment string) string {
	if comment = strings.TrimSpace(comment); comment == "" {
		return automaticRepairComment
	}
	return comment
}

func canRequestRepair(status domain.ShotStatus) bool {
	// Reject duplicate clicks/races even though the generic transition helper
	// deliberately accepts same-status writes for event replay.
	switch status {
	case domain.StatusAwaitingHuman, domain.StatusApproved, domain.StatusFailed, domain.StatusRejected:
		return true
	}
	return false
}

// 修复请求已经写入 RETRYING、但任务未能入队时，立即退回待人工。
// 使用独立短超时上下文，避免客户端断开连接后把镜头永久留在无队列的忙碌态。
func (s *Server) parkUnqueuedRepair(ctx context.Context, jobID, tenant, shotID string, attempt int, cause error) error {
	ctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	defer cancel()
	updated := false
	job, err := s.deps.Store.UpdateJobForTenant(ctx, jobID, tenant, func(j *domain.Job) error {
		shot := j.FindShot(shotID)
		if shot == nil || shot.Attempt != attempt || shot.Status != domain.StatusRetrying {
			return nil
		}
		shot.Status = domain.StatusAwaitingHuman
		shot.Error = "重做任务入队失败：" + cause.Error()
		shot.RecordFeedback(domain.Feedback{Kind: domain.FeedbackSystemError, Source: domain.FeedbackSystem,
			Attempt: attempt, Message: shot.Error, CreatedAt: time.Now().UTC()})
		shot.UpdatedAt = time.Now().UTC()
		if j.Stat().InProgress == 0 {
			j.Status = domain.JobPartial
		}
		updated = true
		return nil
	})
	if err != nil || !updated {
		return err
	}
	_, _ = s.deps.Store.AppendEvent(ctx, &domain.Event{
		JobID: jobID, ShotID: shotID, Node: "hitl", Status: domain.StatusAwaitingHuman,
		Attempt: attempt, Message: "重做任务未能入队，镜头已恢复为待处理，可稍后重试。",
		Error: cause.Error(), Progress: job.ProgressRatio(), Timestamp: time.Now().UTC(),
	})
	return nil
}
