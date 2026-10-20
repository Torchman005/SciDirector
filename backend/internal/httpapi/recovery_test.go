package httpapi

import (
	"net/http"
	"testing"

	"github.com/itJinYu/SciDirector/backend/internal/domain"
)

func TestAutomaticRepairNeedsNoTechnicalCommentAndRejectsDuplicate(t *testing.T) {
	h := newHarness(t)
	job := h.seedJob(t, "automatic-repair", domain.StatusAwaitingHuman)
	path := "/api/v1/jobs/" + job.JobID + "/shots/" + job.Shots[0].ShotID + "/reject"
	w := h.do(t, http.MethodPost, path, map[string]string{})
	if w.Code != http.StatusAccepted {
		t.Fatalf("empty comment rejected: %d %s", w.Code, w.Body.String())
	}
	w = h.do(t, http.MethodPost, path, map[string]string{})
	if w.Code != http.StatusConflict {
		t.Fatalf("duplicate repair accepted: %d %s", w.Code, w.Body.String())
	}
	got, err := h.store.GetJob(h.ctx, job.JobID)
	if err != nil {
		t.Fatal(err)
	}
	fb := got.Shots[0].Feedbacks[0]
	if got.Shots[0].Attempt != 2 || fb.Message != automaticRepairComment || fb.Kind != domain.FeedbackRepairRequest || len(fb.Issues) != 0 || len(fb.Suggestions) != 0 {
		t.Fatalf("unexpected recovery state: %+v", got.Shots[0])
	}
}

func TestRepairQueueFailureReturnsShotToActionableState(t *testing.T) {
	h := newHarness(t)
	job := h.seedJob(t, "queue-failure-repair", domain.StatusAwaitingHuman)
	shotID := job.Shots[0].ShotID
	h.seedVideo(t, job.JobID, shotID)
	if err := h.queue.Close(); err != nil {
		t.Fatal(err)
	}
	path := "/api/v1/jobs/" + job.JobID + "/shots/" + shotID + "/reject"
	w := h.do(t, http.MethodPost, path, map[string]string{})
	if w.Code == http.StatusAccepted {
		t.Fatal("closed queue accepted repair")
	}
	after, err := h.store.GetJob(h.ctx, job.JobID)
	if err != nil {
		t.Fatal(err)
	}
	shot := after.FindShot(shotID)
	if shot.Status != domain.StatusAwaitingHuman || shot.Error == "" || shot.Artifact == nil || shot.Artifact.VideoPath == "" {
		t.Fatalf("enqueue failure stranded repair or lost old video: %+v", shot)
	}
	if after.Status != domain.JobPartial {
		t.Fatalf("job remained busy with no queued repair: %s", after.Status)
	}
}
