package worker

import (
	"context"
	"testing"

	"github.com/itJinYu/SciDirector/backend/internal/domain"
	pb "github.com/itJinYu/SciDirector/backend/internal/pb/scidirector/v1"
)

func TestNarrationAndArtifactPublishTogether(t *testing.T) {
	for _, variant := range []string{"valid", "old-audio", "no-audio", "old-video", "no-video", "partial", "wrong-history"} {
		t.Run(variant, func(t *testing.T) {
			fake := &fakeAIServer{}
			h := newReviseHarness(t, fake)
			ctx := context.Background()
			_, err := h.store.UpdateJob(ctx, h.jobID, func(j *domain.Job) error {
				j.Shots[0].Narration = "original"
				j.Shots[0].Artifact = &domain.Artifact{VideoPath: "old.mp4", AudioPath: "old.mp3"}
				return nil
			})
			if err != nil {
				t.Fatal(err)
			}
			fake.revisionReply = &pb.ReviseShotResponse{Success: true,
				Shot: &pb.ShotSpec{ShotId: h.shotID, Narration: "corrected", VisualBrief: "correct sketch",
					NarrationRevisions: []*pb.NarrationRevision{{Before: "original", After: "corrected", Reason: "verified", TaskIds: []string{"r1"}, Attempt: 2}}},
				Artifact: &pb.RenderArtifact{VideoPath: "new.mp4", AudioPath: "new.mp3", Attempt: 2},
				Feedback: &pb.CriticFeedback{Passed: true, Kind: "content_review"}}
			switch variant {
			case "old-audio":
				fake.revisionReply.Artifact.AudioPath = "old.mp3"
			case "no-audio":
				fake.revisionReply.Artifact.AudioPath = ""
			case "old-video":
				fake.revisionReply.Artifact.VideoPath = "old.mp4"
			case "no-video":
				fake.revisionReply.Artifact.VideoPath = ""
			case "partial":
				fake.revisionReply.PartialRangeHonored = true
			case "wrong-history":
				fake.revisionReply.Shot.NarrationRevisions[0].Before = "not original"
			}
			err = h.deliver(ctx, 2)
			got := h.shot(t)
			if variant != "valid" {
				if err == nil || got.Narration != "original" || got.Artifact.AudioPath != "old.mp3" || got.Status != domain.StatusAwaitingHuman {
					t.Fatalf("invalid revision mixed versions: %+v, %v", got, err)
				}
				return
			}
			if err != nil || got.Narration != "corrected" || got.Artifact.AudioPath != "new.mp3" || len(got.NarrationRevisions) != 1 {
				t.Fatalf("lost semantic update: %+v, %v", got, err)
			}
			job, err := h.store.GetJob(ctx, h.jobID)
			if err != nil {
				t.Fatal(err)
			}
			payload := `{"shots":[{"shot_id":"` + h.shotID + `","narration":"stale director text","visual_brief":"stale sketch"}]}`
			if err := syncShotsFromPayload(job, payload); err != nil {
				t.Fatal(err)
			}
			if job.Shots[0].Narration != "corrected" || len(job.Shots[0].NarrationRevisions) != 1 {
				t.Fatal("plan replay restored stale narration")
			}
		})
	}
}

func TestPipelineSemanticPatchWaitsForRenderedArtifact(t *testing.T) {
	h := newReviseHarness(t, &fakeAIServer{})
	ctx := context.Background()
	_, err := h.store.UpdateJob(ctx, h.jobID, func(j *domain.Job) error {
		j.Shots[0].Narration = "original"
		j.Shots[0].Artifact = &domain.Artifact{VideoPath: "old.mp4", AudioPath: "old.mp3"}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	patch := &pb.ShotSpec{ShotId: h.shotID, Narration: "corrected",
		NarrationRevisions: []*pb.NarrationRevision{{Before: "original", After: "corrected", Reason: "verified", TaskIds: []string{"r1"}, Attempt: 2}}}
	event := &pb.PipelineEvent{ShotId: h.shotID, Node: "code", NarrationPatch: patch}
	if err := h.proc.applyPipelineEvent(ctx, h.jobID, event); err != nil {
		t.Fatal(err)
	}
	if h.shot(t).Narration != "original" {
		t.Fatal("code event published script without media")
	}
	event.Node = "render"
	event.PayloadJson = `{"cost": {`
	event.Artifact = &pb.RenderArtifact{VideoPath: "new.mp4", AudioPath: "new.mp3"}
	if err := h.proc.applyPipelineEvent(ctx, h.jobID, event); err != nil {
		t.Fatal(err)
	}
	got := h.shot(t)
	if got.Narration != "corrected" || got.Artifact.AudioPath != "new.mp3" {
		t.Fatal("render event lost script or new audio", got)
	}
	if err := h.proc.applyPipelineEvent(ctx, h.jobID, event); err != nil {
		t.Fatal("render event replay was not idempotent", err)
	}
}
