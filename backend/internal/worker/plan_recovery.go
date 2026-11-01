package worker

import (
	"context"
	"encoding/json"
	"fmt"

	"google.golang.org/protobuf/encoding/protojson"

	"github.com/itJinYu/SciDirector/backend/internal/domain"
	pb "github.com/itJinYu/SciDirector/backend/internal/pb/scidirector/v1"
	"github.com/itJinYu/SciDirector/backend/internal/pbconv"
)

// syncRecoveredPlan restores missing shots atomically from the checkpoint's
// actual event history. Existing shots keep their progress and review ledger.
func syncRecoveredPlan(ctx context.Context, job *domain.Job, payload string) error {
	var wrap struct {
		RecoveryEvents []json.RawMessage `json:"recovery_events"`
	}
	if err := json.Unmarshal([]byte(payload), &wrap); err != nil {
		return err
	}
	existing := make(map[string]bool, len(job.Shots))
	for _, shot := range job.Shots {
		existing[shot.ShotID] = true
	}
	events := make([]*pb.PipelineEvent, 0, len(wrap.RecoveryEvents))
	for _, raw := range wrap.RecoveryEvents {
		ev := new(pb.PipelineEvent)
		if err := protojson.Unmarshal(raw, ev); err != nil {
			return fmt.Errorf("检查点恢复事件无效: %w", err)
		}
		if ev.GetJobId() != job.JobID || ev.GetShotId() == "" {
			return fmt.Errorf("检查点恢复事件不属于当前任务/分镜")
		}
		switch ev.GetNode() {
		case "code", "render", "critique", "revise":
		default:
			return fmt.Errorf("不支持的检查点恢复事件: %s", ev.GetNode())
		}
		events = append(events, ev)
	}
	// Work on a replacement snapshot. Invalid history must not expose a partly
	// restored plan, even when this helper is used outside Store.UpdateJob.
	recovered := *job
	if err := syncShotsFromPayload(&recovered, payload); err != nil {
		return err
	}
	for _, ev := range events {
		shot := recovered.FindShot(ev.GetShotId())
		if shot == nil {
			return fmt.Errorf("检查点恢复事件引用未知分镜: %s", ev.GetShotId())
		}
		if existing[shot.ShotID] {
			continue
		}
		domainEvent := pbconv.EventFromPipeline(ev)
		if err := applyShotPipelineEvent(ctx, job.JobID, shot, ev, &domainEvent); err != nil {
			return fmt.Errorf("恢复分镜 %s: %w", shot.ShotID, err)
		}
		if recovered.Status == domain.JobPlanning {
			recovered.Status = domain.JobRendering
		}
	}
	*job = recovered
	return nil
}
