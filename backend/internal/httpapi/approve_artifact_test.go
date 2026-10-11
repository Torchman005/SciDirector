package httpapi

import (
	"github.com/itJinYu/SciDirector/backend/internal/domain"
	"net/http"
	"os"
	"path/filepath"
	"testing"
)

func (h *harness) seedVideo(t *testing.T, jobID, shotID string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "video.mp4")
	if err := os.WriteFile(path, []byte("existing artifact"), 0600); err != nil {
		t.Fatal(err)
	}
	_, err := h.store.UpdateJob(h.ctx, jobID, func(j *domain.Job) error { j.FindShot(shotID).Artifact = &domain.Artifact{VideoPath: path}; return nil })
	if err != nil {
		t.Fatal(err)
	}
}

func TestApproveRejectsMissingVideo(t *testing.T) {
	for _, kind := range []string{"none", "missing", "empty"} {
		t.Run(kind, func(t *testing.T) {
			h := newHarness(t)
			job := h.seedJob(t, "no-video", domain.StatusAwaitingHuman)
			if kind != "none" {
				path := filepath.Join(t.TempDir(), "video.mp4")
				if kind == "empty" {
					if err := os.WriteFile(path, nil, 0600); err != nil {
						t.Fatal(err)
					}
				}
				job.Shots[0].Artifact = &domain.Artifact{VideoPath: path}
				if err := h.store.SaveJob(h.ctx, job); err != nil {
					t.Fatal(err)
				}
			}
			w := h.do(t, http.MethodPost, "/api/v1/jobs/no-video/shots/"+job.Shots[0].ShotID+"/approve", nil)
			if w.Code != http.StatusConflict {
				t.Fatalf("expected 409, got %d: %s", w.Code, w.Body.String())
			}
			after, _ := h.store.GetJob(h.ctx, job.JobID)
			if after.Shots[0].Status != domain.StatusAwaitingHuman {
				t.Fatal("approved without video")
			}
		})
	}
}
