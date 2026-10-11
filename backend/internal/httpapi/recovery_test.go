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
	if got.Shots[0].Attempt != 2 || got.Shots[0].Feedbacks[0].Issues[0] != automaticRepairComment {
		t.Fatalf("unexpected recovery state: %+v", got.Shots[0])
	}
}
