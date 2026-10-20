package httpapi

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/itJinYu/SciDirector/backend/internal/domain"
)

func TestShotMediaRangeVersionAndMissingArtifacts(t *testing.T) {
	h := newHarness(t)
	job := h.seedTenantJob(t, "job-media", tenantA)
	dir := t.TempDir()
	video, frame := filepath.Join(dir, "clip.mp4"), filepath.Join(dir, "frame.png")
	if err := os.WriteFile(video, []byte("0123456789"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(frame, []byte("frame-evidence"), 0600); err != nil {
		t.Fatal(err)
	}
	job.Shots[0].Artifact = &domain.Artifact{ArtifactID: "v2", VideoPath: video, FrameSamples: []string{frame}}
	if err := h.store.SaveJob(h.ctx, job); err != nil {
		t.Fatal(err)
	}
	base := "/api/v1/jobs/" + job.JobID + "/shots/" + job.Shots[0].ShotID
	req := httptest.NewRequest(http.MethodGet, base+"/artifact?version=v2", nil)
	req.Header.Set("X-Tenant-ID", tenantA)
	req.Header.Set("Range", "bytes=2-5")
	w := httptest.NewRecorder()
	h.router.ServeHTTP(w, req)
	if w.Code != http.StatusPartialContent || w.Body.String() != "2345" {
		t.Fatalf("range %d: %s", w.Code, w.Body.String())
	}
	for _, tc := range []struct {
		path string
		code int
	}{
		{"/frames/0?version=v2", 200}, {"/frames/-1", 404}, {"/frames/1", 404}, {"/frames/not-a-number", 404},
		{"/artifact?version=v1", 409}, {"/frames/0?version=v1", 409},
	} {
		w := h.asTenant(http.MethodGet, base+tc.path, tenantA, "")
		if w.Code != tc.code {
			t.Errorf("%s: %d %s", tc.path, w.Code, w.Body.String())
		}
	}
	if err := os.Remove(frame); err != nil {
		t.Fatal(err)
	}
	if w := h.asTenant(http.MethodGet, base+"/frames/0", tenantA, ""); w.Code != 404 {
		t.Fatalf("cleaned frame: %d", w.Code)
	}
}

func TestRepairEvidenceUsesStoredIndicesAndTenantOwnership(t *testing.T) {
	h := newHarness(t)
	job := h.seedTenantJob(t, "job-repair-evidence", tenantA)
	before, after := filepath.Join(t.TempDir(), "before.png"), filepath.Join(t.TempDir(), "after.png")
	for path, body := range map[string]string{before: "old frame", after: "new frame"} {
		if err := os.WriteFile(path, []byte(body), 0600); err != nil {
			t.Fatal(err)
		}
	}
	job.Shots[0].RecordFeedback(domain.Feedback{Kind: domain.FeedbackContentReview, Source: domain.FeedbackVLM,
		RepairTasks: []domain.RepairTask{{TaskID: "token", Status: "open", Severity: "major", EvidencePairs: []domain.RepairEvidence{
			{TS: 1, Before: before, After: after, BeforeArtifactID: "v7", AfterArtifactID: "v8"},
		}}}})
	if err := h.store.SaveJob(h.ctx, job); err != nil {
		t.Fatal(err)
	}
	base := "/api/v1/jobs/" + job.JobID + "/shots/" + job.Shots[0].ShotID + "/reviews/"
	for _, tc := range []struct {
		path, body string
		code       int
	}{
		{"0/tasks/0/evidence/0/before", "old frame", 200}, {"0/tasks/0/evidence/0/after", "new frame", 200},
		{"-1/tasks/0/evidence/0/after", "", 404}, {"1/tasks/0/evidence/0/after", "", 404},
		{"0/tasks/-1/evidence/0/after", "", 404}, {"0/tasks/0/evidence/9/after", "", 404},
		{"0/tasks/0/evidence/0/unknown", "", 404}, {"x/tasks/0/evidence/0/after", "", 404},
	} {
		w := h.asTenant(http.MethodGet, base+tc.path, tenantA, "")
		if w.Code != tc.code || (tc.code == 200 && w.Body.String() != tc.body) {
			t.Errorf("%s: %d %s", tc.path, w.Code, w.Body.String())
		}
	}
	if w := h.asTenant(http.MethodGet, base+"0/tasks/0/evidence/0/before", tenantB, ""); w.Code != 404 {
		t.Fatal("cross-tenant evidence leaked")
	}
	if err := os.Remove(after); err != nil {
		t.Fatal(err)
	}
	if w := h.asTenant(http.MethodGet, base+"0/tasks/0/evidence/0/after", tenantA, ""); w.Code != 404 {
		t.Fatal("missing evidence not reported")
	}
}
