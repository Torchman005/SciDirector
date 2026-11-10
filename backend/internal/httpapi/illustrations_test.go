package httpapi

import (
	"bytes"
	"context"
	"encoding/json"
	"image"
	"image/color"
	"image/png"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/itJinYu/SciDirector/backend/internal/ai"
	"github.com/itJinYu/SciDirector/backend/internal/assets"
	"github.com/itJinYu/SciDirector/backend/internal/config"
	pb "github.com/itJinYu/SciDirector/backend/internal/pb/scidirector/v1"
	"google.golang.org/grpc"
)

type illustrationAI struct {
	pb.UnimplementedAiDirectorServiceServer
	calls   int
	staging string
}

func (f *illustrationAI) GetImageProviders(context.Context, *pb.ImageProvidersRequest) (*pb.ImageProvidersResponse, error) {
	return &pb.ImageProvidersResponse{ProvidersJson: `{"providers":[{"id":"relay","label":"Relay","model":"fixture","configured":true,"sizes":["1024x1024"]}]}`}, nil
}
func (f *illustrationAI) RecommendFyStyle(context.Context, *pb.RecommendFyStyleRequest) (*pb.RecommendFyStyleResponse, error) {
	return &pb.RecommendFyStyleResponse{RecommendationJson: `{"style_id":"X17","method":"catalog_keyword_match","library_version":"fixture","reason":"匹配原理","alternatives":[]}`}, nil
}
func (f *illustrationAI) GenerateIllustration(_ context.Context, req *pb.GenerateIllustrationRequest) (*pb.GenerateIllustrationResponse, error) {
	f.calls++
	f.staging = req.OutputDir
	path := filepath.Join(req.OutputDir, "illustration.png")
	file, err := os.Create(path)
	if err != nil {
		return nil, err
	}
	err = png.Encode(file, image.NewRGBA(image.Rect(0, 0, 32, 24)))
	_ = file.Close()
	if err != nil {
		return nil, err
	}
	return &pb.GenerateIllustrationResponse{ImagePath: path, ProviderId: req.ProviderId, Model: "fixture", FyStyleId: req.FyStyleId, FyStyleSha256: "fixture-source"}, nil
}

// This isolated HTTP/gRPC fixture deliberately never creates or flushes Redis.
func illustrationRouter(t *testing.T) (*gin.Engine, *Server, *illustrationAI) {
	t.Helper()
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	upstream := &illustrationAI{}
	gs := grpc.NewServer()
	pb.RegisterAiDirectorServiceServer(gs, upstream)
	go func() { _ = gs.Serve(lis) }()
	t.Cleanup(gs.Stop)
	client, err := ai.NewClient(config.AIConfig{Addr: lis.Addr().String(), UnaryTimeout: time.Second}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Close() })
	cfg := &config.Config{Env: "test", Media: config.MediaConfig{WorkDir: t.TempDir()}, Tenant: config.TenantConfig{Mode: "header", Header: "X-Tenant-ID"}}
	deps := Deps{Config: cfg, AI: client}
	server := NewServer(deps)
	return NewRouter(server, deps), server, upstream
}

func imageRequest(t *testing.T, router *gin.Engine, tenant, method, path string, body any) *httptest.ResponseRecorder {
	t.Helper()
	encoded, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest(method, path, bytes.NewReader(encoded))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Tenant-ID", tenant)
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)
	return w
}

func TestIllustrationHTTPGRPCIngestionAndTenantAssetsWithoutRedis(t *testing.T) {
	router, server, upstream := illustrationRouter(t)
	for _, path := range []string{"/api/v1/image-providers"} {
		w := imageRequest(t, router, "a", http.MethodGet, path, nil)
		if w.Code != http.StatusOK {
			t.Fatalf("%d %s", w.Code, w.Body)
		}
	}
	w := imageRequest(t, router, "a", http.MethodPost, "/api/v1/styles/fy/recommend", map[string]any{"raw_script": "通过一台蒸汽机的机械结构拆解，逐步讲解它的工作原理。"})
	if w.Code != http.StatusOK {
		t.Fatalf("recommend: %d %s", w.Code, w.Body)
	}
	req := illustrationRequest{ProviderID: "relay", Prompt: "一台蒸汽机", Size: "1024x1024", Role: "prop", FyStyleID: "X17"}
	w = imageRequest(t, router, "a", http.MethodPost, "/api/v1/assets/visuals/generate", req)
	if w.Code != http.StatusBadRequest || upstream.calls != 0 {
		t.Fatalf("confirmation: %d %s", w.Code, w.Body)
	}
	req.ConfirmCost = true
	w = imageRequest(t, router, "a", http.MethodPost, "/api/v1/assets/visuals/generate", req)
	if w.Code != http.StatusOK {
		t.Fatalf("generate: %d %s", w.Code, w.Body)
	}
	asset := decodeOK[assets.VisualAsset](t, w)
	if upstream.calls != 1 || asset.Generated == nil || asset.Generated.Model != "fixture" || asset.Generated.FyStyleID != "X17" {
		t.Fatalf("%+v", asset)
	}
	if asset.Description != req.Prompt {
		t.Fatal("generated asset must retain a useful description for the director")
	}
	if _, err := os.Stat(upstream.staging); !os.IsNotExist(err) {
		t.Fatalf("staging was not cleaned: %v", err)
	}
	if w := imageRequest(t, router, "a", http.MethodGet, "/api/v1/assets/visuals/"+asset.AssetID, nil); w.Code != http.StatusOK {
		t.Fatalf("preview: %d", w.Code)
	}
	if w := imageRequest(t, router, "b", http.MethodGet, "/api/v1/assets/visuals/"+asset.AssetID, nil); w.Code != http.StatusNotFound {
		t.Fatalf("tenant scope: %d", w.Code)
	}
	guide := map[string]any{"animation_style": "fy", "fy_style_id": "X17", "scene_assets": []any{map[string]any{"asset_id": asset.AssetID}}}
	if err := validateStyleGuide(guide); err != nil {
		t.Fatal(err)
	}
	if err := server.resolveVisualSelections("a", guide); err != nil {
		t.Fatal(err)
	}
	if guide["scene_assets"].([]assets.VisualAsset)[0].AssetID != asset.AssetID {
		t.Fatal("lost selected image")
	}
}

func TestIllustrationRequestRequiresConfirmationAndMetadata(t *testing.T) {
	req := illustrationRequest{ProviderID: "relay", Prompt: "一台蒸汽机", Size: "1024x1024", Role: "prop"}
	if req.validate() == nil {
		t.Fatal("cost confirmation required")
	}
	req.ConfirmCost = true
	if err := req.validate(); err != nil {
		t.Fatal(err)
	}
	req.Pose = "转动"
	if req.validate() == nil {
		t.Fatal("pose requires subject")
	}
	req.Subject = "蒸汽机"
	if err := req.validate(); err != nil {
		t.Fatal(err)
	}
}

func TestIllustrationAndUploadShareFullPixelValidation(t *testing.T) {
	var encoded bytes.Buffer
	img := image.NewRGBA(image.Rect(0, 0, 12, 8))
	img.Set(1, 1, color.RGBA{R: 230, G: 60, B: 80, A: 255})
	if err := png.Encode(&encoded, img); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "image.png")
	if err := os.WriteFile(path, encoded.Bytes(), 0600); err != nil {
		t.Fatal(err)
	}
	asset, ext, err := inspectVisual(path, "illustration.png")
	if err != nil || ext != ".png" || asset.Width != 12 || asset.Height != 8 {
		t.Fatalf("%+v %s %v", asset, ext, err)
	}
	if err := os.WriteFile(path, encoded.Bytes()[:35], 0600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := inspectVisual(path, "illustration.png"); err == nil {
		t.Fatal("incomplete pixels accepted")
	}
}
