package httpapi

import (
	"net/http"
	"testing"
)

func TestFyCatalogAndSubmissionPersistResolvedSource(t *testing.T) {
	h := newHarnessWithQuota(t, 0, "tool:playwright=ok")
	catalog := h.do(t, http.MethodGet, "/api/v1/styles/fy", nil)
	if catalog.Code != http.StatusOK {
		t.Fatalf("catalog: %d %s", catalog.Code, catalog.Body)
	}
	w := h.do(t, http.MethodPost, "/api/v1/generate", map[string]any{
		"raw_script": "从输入经过处理再到输出，通过三个阶段讲清楚整个过程。", "target_duration_sec": 15,
		"style_guide": map[string]any{"animation_style": "fy", "fy_style_id": "X17", "preset": "paper", "background_style": "paper_stage"},
	})
	if w.Code != http.StatusAccepted {
		t.Fatalf("generate: %d %s", w.Code, w.Body)
	}
	response := decodeOK[GenerateResponse](t, w)
	job, err := h.store.GetJob(h.ctx, response.JobID)
	if err != nil {
		t.Fatal(err)
	}
	source, ok := job.StyleGuide["fy_style_source"].(map[string]any)
	if !ok || source["content"] != "完整画风原文" || source["sha256"] != "test-sha" {
		t.Fatalf("source not persisted: %#v", job.StyleGuide)
	}
	if _, exists := job.StyleGuide["preset"]; exists {
		t.Fatal("legacy palette competes with fy")
	}
	if _, exists := job.StyleGuide["background_style"]; exists {
		t.Fatal("legacy background competes with fy")
	}
}

func TestFyUnavailableNeverCreatesDefaultStyleJob(t *testing.T) {
	for _, capabilities := range [][]string{nil, {"tool:playwright=ok"}} {
		h := newHarnessWithQuota(t, 0, capabilities...)
		id := "X17"
		if len(capabilities) > 0 {
			id = "X999"
		}
		w := h.do(t, http.MethodPost, "/api/v1/generate", map[string]any{
			"raw_script":  "从输入经过处理再到输出，通过三个阶段讲清楚整个过程。",
			"style_guide": map[string]any{"animation_style": "fy", "fy_style_id": id},
		})
		if w.Code != http.StatusServiceUnavailable {
			t.Fatalf("expected explicit failure: %d %s", w.Code, w.Body)
		}
	}
}

func TestFyRequestAcceptsOnlyServerResolvedSources(t *testing.T) {
	cases := []map[string]any{
		{"animation_style": "fy"},
		{"animation_style": "fy", "fy_style_id": 17},
		{"animation_style": "fy", "fy_style_id": "../X17"},
		{"animation_style": "fy", "fy_style_id": "X17", "fy_style_source": map[string]any{"content": "client prompt"}},
		{"animation_style": "anime", "fy_style_id": "X17"},
	}
	for _, sg := range cases {
		if err := validateStyleGuide(sg); err == nil {
			t.Fatalf("accepted invalid style: %#v", sg)
		}
	}
	if err := validateStyleGuide(map[string]any{"animation_style": "fy", "fy_style_id": "X17"}); err != nil {
		t.Fatal(err)
	}
}
