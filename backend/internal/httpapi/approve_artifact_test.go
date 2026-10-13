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

func TestFailedShotCanBeReworkedOrApprovedWithVideo(t *testing.T) {
	for _, status := range []domain.ShotStatus{domain.StatusFailed, domain.StatusRejected} {
		t.Run(string(status), func(t *testing.T) {
			h := newHarness(t)
			job := h.seedJob(t, "review-"+string(status), status)
			job.Status = domain.JobFailed
			job.Error = "旧任务失败"
			if err := h.store.SaveJob(h.ctx, job); err != nil {
				t.Fatal(err)
			}
			path := "/api/v1/jobs/" + job.JobID + "/shots/" + job.Shots[0].ShotID
			if w := h.do(t, http.MethodPost, path+"/approve", nil); w.Code != http.StatusConflict {
				t.Fatalf("无视频不能放行: %d %s", w.Code, w.Body.String())
			}
			if w := h.do(t, http.MethodPost, path+"/reject", map[string]string{}); w.Code != http.StatusAccepted {
				t.Fatalf("失败后应可打回重做: %d %s", w.Code, w.Body.String())
			}
			after, err := h.store.GetJob(h.ctx, job.JobID)
			if err != nil || after.Shots[0].Status != domain.StatusRetrying || after.Status != domain.JobRendering || after.Error != "" {
				t.Fatalf("打回后状态未恢复: job=%+v err=%v", after, err)
			}
			// 模拟这轮重做仍失败，但保留了上一版视频。
			if _, err := h.store.UpdateJob(h.ctx, job.JobID, func(j *domain.Job) error {
				j.Status = domain.JobPartial
				j.FindShot(job.Shots[0].ShotID).Status = status
				return nil
			}); err != nil {
				t.Fatal(err)
			}
			h.seedVideo(t, job.JobID, job.Shots[0].ShotID)
			w := h.do(t, http.MethodPost, path+"/approve", nil)
			if w.Code != http.StatusOK {
				t.Fatalf("有视频应可人工放行: %d %s", w.Code, w.Body.String())
			}
			approved, err := h.store.GetJob(h.ctx, job.JobID)
			if err != nil || approved.Shots[0].Status != domain.StatusApproved || approved.Status != domain.JobComposing {
				t.Fatalf("放行后未继续合成: job=%+v err=%v", approved, err)
			}
		})
	}
}
