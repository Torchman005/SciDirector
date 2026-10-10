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
