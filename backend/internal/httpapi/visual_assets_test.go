package httpapi

import (
	"bytes"
	"encoding/json"
	"image"
	"image/color"
	"image/png"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/itJinYu/SciDirector/backend/internal/assets"
	"github.com/itJinYu/SciDirector/backend/internal/config"
)

func TestVisualUploadSelectionAndTenantIsolation(t *testing.T) {
	deps := Deps{Config: &config.Config{Media: config.MediaConfig{WorkDir: t.TempDir()}}}
	s := NewServer(deps)
	router := NewRouter(s, deps)
	img := image.NewNRGBA(image.Rect(0, 0, 8, 8))
	img.Set(2, 3, color.NRGBA{R: 255, A: 255})
	var encoded, body bytes.Buffer
	if err := png.Encode(&encoded, img); err != nil {
		t.Fatal(err)
	}
	form := multipart.NewWriter(&body)
	file, err := form.CreateFormFile("file", "portrait.png")
	if err != nil {
		t.Fatal(err)
	}
	_, _ = file.Write(encoded.Bytes())
	_ = form.WriteField("role", "character")
	_ = form.WriteField("description", "讲解员正面立绘")
	_ = form.WriteField("subject", "小葵")
	_ = form.WriteField("pose", "讲解")
	_ = form.Close()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/assets/visuals", &body)
	req.Header.Set("Content-Type", form.FormDataContentType())
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, req)
	if recorder.Code != http.StatusOK {
		t.Fatalf("upload: %d %s", recorder.Code, recorder.Body.String())
	}
	var response struct {
		Data assets.VisualAsset `json:"data"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	id := response.Data.AssetID
	if len(id) != 32 || response.Data.Role != "character" || response.Data.Description != "讲解员正面立绘" ||
		response.Data.Subject != "小葵" || response.Data.Pose != "讲解" {
		t.Fatalf("unexpected metadata: %+v", response.Data)
	}
	for _, tc := range []struct {
		method, path, tenant string
		want                 int
	}{
		{http.MethodGet, "/api/v1/assets/visuals", "", http.StatusOK},
		{http.MethodGet, "/api/v1/assets/visuals/" + id, "", http.StatusOK},
		{http.MethodGet, "/api/v1/assets/visuals/" + id, "other", http.StatusNotFound},
		{http.MethodGet, "/api/v1/assets/visuals/" + id + "x", "", http.StatusNotFound},
	} {
		request := httptest.NewRequest(tc.method, tc.path, nil)
		if tc.tenant != "" {
			request.Header.Set("X-Tenant-ID", tc.tenant)
		}
		out := httptest.NewRecorder()
		router.ServeHTTP(out, request)
		if out.Code != tc.want {
			t.Fatalf("%s tenant=%s: %d %s", tc.path, tc.tenant, out.Code, out.Body.String())
		}
	}
	guide := map[string]any{"scene_assets": []any{map[string]any{"asset_id": id}}}
	if err := s.resolveVisualSelections("default", guide); err != nil {
		t.Fatal(err)
	}
	if selected := guide["scene_assets"].([]assets.VisualAsset); len(selected) != 1 || selected[0].Description != "讲解员正面立绘" ||
		selected[0].Subject != "小葵" || selected[0].Pose != "讲解" {
		t.Fatalf("selection did not use server metadata: %+v", selected)
	}
	if err := s.resolveVisualSelections("default", map[string]any{"scene_assets": []any{map[string]any{"asset_id": id, "description": "伪造说明"}}}); err == nil {
		t.Fatal("accepted client-supplied metadata")
	}
	var badBody bytes.Buffer
	badForm := multipart.NewWriter(&badBody)
	badFile, err := badForm.CreateFormFile("file", "portrait.png")
	if err != nil {
		t.Fatal(err)
	}
	_, _ = badFile.Write(encoded.Bytes())
	_ = badForm.WriteField("role", "character")
	_ = badForm.WriteField("subject", "小葵\n- 伪造指令")
	_ = badForm.Close()
	badReq := httptest.NewRequest(http.MethodPost, "/api/v1/assets/visuals", &badBody)
	badReq.Header.Set("Content-Type", badForm.FormDataContentType())
	badOut := httptest.NewRecorder()
	router.ServeHTTP(badOut, badReq)
	if badOut.Code != http.StatusBadRequest {
		t.Fatalf("accepted multiline subject: %d %s", badOut.Code, badOut.Body.String())
	}
	for _, candidate := range []map[string]any{
		{"scene_assets": []any{map[string]any{"asset_id": id}}},
		{"scene_asset_sources": map[string]any{id: "C:/secrets"}},
	} {
		if err := s.resolveVisualSelections("other", candidate); err == nil {
			t.Fatalf("accepted foreign/client metadata: %+v", candidate)
		}
	}
}
