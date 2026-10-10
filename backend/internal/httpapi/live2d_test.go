package httpapi

import (
	"archive/zip"
	"bytes"
	"encoding/json"
	"image"
	"image/png"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/itJinYu/SciDirector/backend/internal/assets"
	"github.com/itJinYu/SciDirector/backend/internal/config"
	"github.com/itJinYu/SciDirector/backend/internal/domain"
)

func TestLive2DHTTPImportAndServerOwnedResolution(t *testing.T) {
	gin.SetMode(gin.TestMode)
	s := NewServer(Deps{Config: &config.Config{Media: config.MediaConfig{WorkDir: t.TempDir()}}})
	var tex, archive, body bytes.Buffer
	if err := png.Encode(&tex, image.NewRGBA(image.Rect(0, 0, 2, 2))); err != nil {
		t.Fatal(err)
	}
	z := zip.NewWriter(&archive)
	for name, data := range map[string][]byte{
		"a.model3.json": []byte(`{"Version":3,"FileReferences":{"Moc":"a.moc3","Textures":["a.png"]}}`),
		"a.moc3":        []byte("MOC3test"), "a.png": tex.Bytes(),
	} {
		w, err := z.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		_, _ = w.Write(data)
	}
	if err := z.Close(); err != nil {
		t.Fatal(err)
	}
	m := multipart.NewWriter(&body)
	w, err := m.CreateFormFile("file", "character.zip")
	if err != nil {
		t.Fatal(err)
	}
	_, _ = w.Write(archive.Bytes())
	_ = m.Close()
	req := httptest.NewRequest(http.MethodPost, "/", &body)
	req.Header.Set("Content-Type", m.FormDataContentType())
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = req
	s.HandleUploadLive2D(c)
	if recorder.Code != http.StatusOK {
		t.Fatalf("%d: %s", recorder.Code, recorder.Body.String())
	}
	var result struct {
		Data assets.Live2DAsset `json:"data"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	input := domain.Effects{Presenter: &domain.PresenterEffects{AssetID: result.Data.AssetID, ModelPath: "C:/private/secret.model3.json"}}
	resolved, err := s.resolveEffects(c, tenantOf(c), input)
	if err != nil {
		t.Fatal(err)
	}
	if resolved.Presenter.ModelPath == input.Presenter.ModelPath || !strings.HasSuffix(resolved.Presenter.ModelPath, ".model3.json") {
		t.Fatal("client path trusted")
	}
	if _, err := os.Stat(resolved.Presenter.ModelPath); err != nil {
		t.Fatal(err)
	}
	foreign, _ := gin.CreateTestContext(httptest.NewRecorder())
	foreign.Request = httptest.NewRequest(http.MethodGet, "/", nil)
	if _, err := s.resolveEffects(foreign, "other-tenant", input); err == nil {
		t.Fatal("foreign asset accepted")
	}
}
