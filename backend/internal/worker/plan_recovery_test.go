package worker

import (
	"context"
	"encoding/json"
	"os"
	"testing"

	"github.com/itJinYu/SciDirector/backend/internal/domain"
	pb "github.com/itJinYu/SciDirector/backend/internal/pb/scidirector/v1"
)

func recoveryPayload(t *testing.T) string {
	t.Helper()
	raw, err := os.ReadFile("../../../proto/testdata/pipeline_plan_recovery.json")
	if err != nil {
		t.Fatal(err)
	}
	return string(raw)
}

func assertRecoveredShots(t *testing.T, job *domain.Job) {
	t.Helper()
	if len(job.Shots) != 3 || job.Status != domain.JobRendering {
		t.Fatalf("lost plan: status=%s shots=%d", job.Status, len(job.Shots))
	}
	first, second, pending := job.Shots[0], job.Shots[1], job.Shots[2]
	if first.Status != domain.StatusApproved || first.Attempt != 2 || first.Code != "latest source" || first.Language != "html+js" {
		t.Fatalf("lost latest code/status: %+v", first)
	}
	if first.Narration != "corrected" || len(first.NarrationRevisions) != 1 || first.Artifact.VideoPath != "video-2.mp4" || first.Artifact.AudioPath != "audio-2.mp3" {
		t.Fatalf("mixed narration and media versions: %+v", first)
	}
	if first.Artifact.RenderedAt.UnixMilli() != 1700000000002 {
		t.Fatal("protobuf int64 timestamp was not restored")
	}
	if len(first.Feedbacks) != 2 || len(first.RepairTasks) != 1 || first.RepairTasks[0].Status != "resolved" {
		t.Fatalf("lost actual review history/ledger: %+v", first)
	}
	if second.Status != domain.StatusAwaitingHuman || second.Code != "second source" || second.Error != "layout failed" || len(second.Feedbacks) != 1 {
		t.Fatalf("lost failure/handoff: %+v", second)
	}
	if pending.Status != domain.StatusPending || pending.Artifact != nil || pending.Attempt != 0 {
		t.Fatalf("unstarted shot was fabricated as complete: %+v", pending)
	}
	for _, shot := range job.Shots {
		if shot.Engine != domain.EngineMotion {
			t.Fatalf("fy routing changed after recovery: %s=%s", shot.ShotID, shot.Engine)
		}
	}
	if stat := job.Stat(); stat.Total != 3 || stat.Approved != 1 || stat.AwaitingHuman != 1 {
		t.Fatalf("wrong storyboard counts: %+v", stat)
	}
}

func TestRecoverPythonCheckpointPlanAndActualHistory(t *testing.T) {
	job := &domain.Job{JobID: "job-x", Status: domain.JobPlanning,
		StyleGuide: map[string]any{"animation_style": "fy"}}
	payload := recoveryPayload(t)
	if err := syncRecoveredPlan(context.Background(), job, payload); err != nil {
		t.Fatal(err)
	}
	assertRecoveredShots(t, job)
	if err := syncRecoveredPlan(context.Background(), job, payload); err != nil {
		t.Fatal(err)
	}
	assertRecoveredShots(t, job) // no duplicate review or lost latest source
}

func TestRecoveryPreservesExistingProgressAndRepairsMissingSlots(t *testing.T) {
	job := &domain.Job{JobID: "job-x", Status: domain.JobRendering,
		StyleGuide: map[string]any{"animation_style": "fy"}, Shots: []*domain.Shot{{
			ShotID: "job-x-s000", Status: domain.StatusApproved, Attempt: 5,
			Code: "newer source", Language: "html+js", Narration: "human corrected",
			NarrationRevisions: []domain.NarrationRevision{{Before: "original", After: "human corrected"}},
			Artifact:           &domain.Artifact{VideoPath: "newer.mp4"},
			Feedbacks:          []domain.Feedback{{Kind: "human_review", Passed: true}},
		}}}
	if err := syncRecoveredPlan(context.Background(), job, recoveryPayload(t)); err != nil {
		t.Fatal(err)
	}
	first := job.Shots[0]
	if first.Code != "newer source" || first.Attempt != 5 || first.Artifact.VideoPath != "newer.mp4" ||
		first.Narration != "human corrected" || len(first.Feedbacks) != 1 {
		t.Fatalf("rolled back existing progress: %+v", first)
	}
	if len(job.Shots) != 3 || job.Shots[1].Status != domain.StatusAwaitingHuman {
		t.Fatal("did not fill missing slots")
	}
}

func TestInvalidRecoveryNeverPublishesPartialPlan(t *testing.T) {
	for _, variant := range []string{"foreign-job", "unknown-shot", "unsupported-node", "bad-proto", "bad-narration"} {
		t.Run(variant, func(t *testing.T) {
			var payload map[string]any
			if err := json.Unmarshal([]byte(recoveryPayload(t)), &payload); err != nil {
				t.Fatal(err)
			}
			events := payload["recovery_events"].([]any)
			// Damage a later event so an earlier event has already been replayed.
			event := events[4].(map[string]any)
			switch variant {
			case "foreign-job":
				event["job_id"] = "other-job"
			case "unknown-shot":
				event["shot_id"] = "unknown"
			case "unsupported-node":
				event["node"] = "compose"
			case "bad-proto":
				event["ts_unix_ms"] = "not-an-integer"
			case "bad-narration":
				patch := event["narration_patch"].(map[string]any)
				patch["narration_revisions"].([]any)[0].(map[string]any)["before"] = "unrelated text"
			}
			raw, _ := json.Marshal(payload)
			job := &domain.Job{JobID: "job-x", Status: domain.JobPlanning, Shots: []*domain.Shot{{ShotID: "keep", Code: "current"}}}
			before, _ := json.Marshal(job)
			if err := syncRecoveredPlan(context.Background(), job, string(raw)); err == nil {
				t.Fatal("invalid checkpoint accepted")
			}
			after, _ := json.Marshal(job)
			if string(after) != string(before) {
				t.Fatal("partly restored plan escaped after failure")
			}
		})
	}
}

func TestRecoveredApprovalStillRequiresVideo(t *testing.T) {
	job := &domain.Job{JobID: "job-x", Status: domain.JobPlanning}
	payload := `{"shots":[{"shot_id":"s"}],"recovery_events":[{"job_id":"job-x","shot_id":"s","node":"critique","status":5,"feedback":{"passed":true,"kind":"content_review"}}]}`
	if err := syncRecoveredPlan(context.Background(), job, payload); err != nil {
		t.Fatal(err)
	}
	if job.Shots[0].Status != domain.StatusAwaitingHuman || job.Shots[0].Error == "" {
		t.Fatal("recovery bypassed the video requirement")
	}
}

func TestRecoveryPlanEventPersistsWithoutResettingCurrentShot(t *testing.T) {
	proc, st, ctx := newCostHarness(t)
	job := &domain.Job{JobID: "job-x", Status: domain.JobPlanning,
		StyleGuide: map[string]any{"animation_style": "fy"}}
	if err := st.SaveJob(ctx, job); err != nil {
		t.Fatal(err)
	}
	ev := &pb.PipelineEvent{JobId: job.JobID, Node: "plan", PayloadJson: recoveryPayload(t)}
	for i := 0; i < 2; i++ {
		if err := proc.applyPipelineEvent(ctx, job.JobID, ev); err != nil {
			t.Fatal(err)
		}
		got, err := st.GetJob(ctx, job.JobID)
		if err != nil {
			t.Fatal(err)
		}
		assertRecoveredShots(t, got)
	}
}

func TestUnknownShotAndBadPlanReturnErrors(t *testing.T) {
	proc, st, ctx := newCostHarness(t)
	job := &domain.Job{JobID: "job-x", Status: domain.JobPlanning}
	if err := st.SaveJob(ctx, job); err != nil {
		t.Fatal(err)
	}
	for _, ev := range []*pb.PipelineEvent{
		{JobId: job.JobID, ShotId: "missing", Node: "render", Status: pb.ShotStatus_SHOT_STATUS_APPROVED},
		{JobId: job.JobID, Node: "plan", PayloadJson: `{"shots":[{"index":"invalid"}]}`},
	} {
		if err := proc.applyPipelineEvent(ctx, job.JobID, ev); err == nil {
			t.Fatal("event discarded without reporting a synchronization error")
		}
		got, err := st.GetJob(ctx, job.JobID)
		if err != nil || len(got.Shots) != 0 || got.Status != domain.JobPlanning {
			t.Fatalf("failed sync changed stored job: %+v, %v", got, err)
		}
	}
}
