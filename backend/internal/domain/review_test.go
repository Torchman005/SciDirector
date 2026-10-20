package domain

import (
	"encoding/json"
	"reflect"
	"testing"
)

func reviewTask() RepairTask {
	return RepairTask{TaskID: "r7-01", Target: "token", Severity: "major", Status: "partial",
		Evidence: "label clipped", Instruction: "separate label", Acceptance: "label completely visible",
		ResolutionEvidence: "left side readable"}
}

func TestRepairRequestAndOutageCannotOverwriteLedger(t *testing.T) {
	task := reviewTask()
	shot := &Shot{Artifact: &Artifact{ArtifactID: "v7"}}
	shot.RecordFeedback(Feedback{Source: FeedbackVLM, Kind: FeedbackContentReview, Issues: []string{"clipped"}, RepairTasks: []RepairTask{task}})
	shot.RecordFeedback(Feedback{Source: FeedbackHuman, Kind: FeedbackRepairRequest, Message: "automatic repair"})
	shot.RecordFeedback(Feedback{Source: FeedbackSystem, Kind: FeedbackSystemError, Message: "renderer timeout"})
	wrong := task
	wrong.Acceptance = "rewritten goal"
	wrong.Status, wrong.ResolutionEvidence = "resolved", "model says done"
	wrong.EvidencePairs = []RepairEvidence{{TS: 1, Before: "before.png", After: "after.png"}}
	shot.RecordFeedback(Feedback{Source: FeedbackSystem, Kind: FeedbackReviewUnavailable, RepairTasks: []RepairTask{wrong}})
	got := shot.RepairTasks[0]
	if got.Status != task.Status || got.Acceptance != task.Acceptance || got.ResolutionEvidence != task.ResolutionEvidence || len(got.EvidencePairs) != 1 {
		t.Fatalf("outage changed task obligations: %+v", got)
	}
	if !reflect.DeepEqual(shot.Feedbacks[0].RepairTasks[0], task) {
		t.Fatal("original evidence mutated")
	}
	ctx := shot.RepairFeedback()
	if ctx.Kind != FeedbackContentReview || ctx.Issues[0] != "clipped" || len(ctx.RepairTasks) != 1 {
		t.Fatalf("lost repair context: %+v", ctx)
	}
	raw, err := json.Marshal(shot)
	if err != nil {
		t.Fatal(err)
	}
	var restored Shot
	if err := json.Unmarshal(raw, &restored); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(restored.RepairTasks, shot.RepairTasks) {
		t.Fatal("ledger lost after persistence")
	}
}

func TestLegacyLedgerAndMissingTasksCannotBeSilentlyApproved(t *testing.T) {
	shot := &Shot{Feedbacks: []Feedback{{Source: FeedbackVLM, RepairTasks: []RepairTask{reviewTask()}},
		{Source: FeedbackSystem, Issues: []string{"VLM unavailable"}}}}
	if got := shot.RepairFeedback(); got == nil || len(got.RepairTasks) != 1 {
		t.Fatal("latest outage hid legacy task")
	}
	shot.RecordFeedback(Feedback{Source: FeedbackVLM, Passed: true, Score: .99})
	if shot.Feedbacks[2].Passed || len(shot.RepairTasks) != 1 {
		t.Fatal("high score erased original obligation")
	}
	fixed := reviewTask()
	fixed.Status, fixed.ResolutionEvidence = "resolved", "complete label visible in AFTER 4"
	shot.RecordFeedback(Feedback{Source: FeedbackVLM, Passed: true, RepairTasks: []RepairTask{fixed}})
	if !shot.Feedbacks[3].Passed || shot.RepairTasks[0].Status != "resolved" {
		t.Fatal("valid resolution rejected")
	}
}

func TestLegacyLedgerIgnoresFaultStatusChanges(t *testing.T) {
	task := reviewTask()
	wrong := task
	wrong.Status, wrong.ResolutionEvidence = "resolved", "unverified closure"
	wrong.EvidencePairs = []RepairEvidence{{TS: 2, Before: "before.png", After: "after.png"}}
	shot := &Shot{Feedbacks: []Feedback{
		{Source: FeedbackVLM, RepairTasks: []RepairTask{task}},
		{Kind: FeedbackReviewUnavailable, Source: FeedbackSystem, RepairTasks: []RepairTask{wrong}},
		{Kind: FeedbackSystemError, Source: FeedbackSystem, RepairTasks: []RepairTask{wrong}},
	}}
	got := shot.RepairLedger()[0]
	if got.Status != task.Status || got.ResolutionEvidence != task.ResolutionEvidence || len(got.EvidencePairs) != 1 {
		t.Fatalf("legacy faults changed verified task state: %+v", got)
	}
}
