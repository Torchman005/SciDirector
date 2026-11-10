package httpapi

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/gin-gonic/gin"
	"github.com/itJinYu/SciDirector/backend/internal/assets"
	pb "github.com/itJinYu/SciDirector/backend/internal/pb/scidirector/v1"
)

type illustrationRequest struct {
	ProviderID  string `json:"provider_id"`
	Prompt      string `json:"prompt"`
	Size        string `json:"size"`
	FyStyleID   string `json:"fy_style_id"`
	Role        string `json:"role"`
	Description string `json:"description"`
	Subject     string `json:"subject"`
	Pose        string `json:"pose"`
	ConfirmCost bool   `json:"confirm_cost"`
}

func (r illustrationRequest) validate() error {
	if !r.ConfirmCost {
		return fmt.Errorf("请先确认本次生图可能产生服务商费用")
	}
	if !regexp.MustCompile(`^[a-z][a-z0-9_-]{0,31}$`).MatchString(r.ProviderID) || utf8.RuneCountInString(strings.TrimSpace(r.Prompt)) < 4 || utf8.RuneCountInString(r.Prompt) > 4000 {
		return fmt.Errorf("服务商或生图描述无效")
	}
	if !regexp.MustCompile(`^[1-9][0-9]{2,3}x[1-9][0-9]{2,3}$`).MatchString(r.Size) {
		return fmt.Errorf("图片尺寸无效")
	}
	if r.FyStyleID != "" && !regexp.MustCompile(`^[A-Z]+[0-9]+$`).MatchString(r.FyStyleID) {
		return fmt.Errorf("画风编号无效")
	}
	if r.Role != "character" && r.Role != "prop" && r.Role != "background" {
		return fmt.Errorf("素材用途必须为角色、道具或背景")
	}
	if utf8.RuneCountInString(r.Description) > 240 || utf8.RuneCountInString(r.Subject) > 48 || utf8.RuneCountInString(r.Pose) > 32 || (r.Pose != "" && r.Subject == "") || strings.IndexFunc(r.Subject+r.Pose, unicode.IsControl) >= 0 {
		return fmt.Errorf("素材说明、对象名称或姿态无效")
	}
	return nil
}

func (s *Server) HandleImageProviders(c *gin.Context) {
	resp, err := s.deps.AI.GetImageProviders(c.Request.Context())
	if err != nil || !json.Valid([]byte(resp.GetProvidersJson())) {
		abortWith(c, http.StatusServiceUnavailable, ErrCodeUpstream, "生图服务列表不可用，请检查 AI 服务版本", nil)
		return
	}
	respondOK(c, json.RawMessage(resp.ProvidersJson))
}

func (s *Server) HandleGenerateIllustration(c *gin.Context) {
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 32<<10)
	var req illustrationRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, "生图请求无效", nil)
		return
	}
	req.Subject, req.Pose = strings.TrimSpace(req.Subject), strings.TrimSpace(req.Pose)
	if strings.TrimSpace(req.Description) == "" {
		description := []rune(strings.TrimSpace(req.Prompt))
		if len(description) > 240 {
			description = description[:240]
		}
		req.Description = string(description)
	}
	if err := req.validate(); err != nil {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, err.Error(), nil)
		return
	}
	store, err := s.assetStore()
	if err != nil {
		mapError(c, err)
		return
	}
	items, err := store.ListVisuals(tenantOf(c))
	if err != nil {
		mapError(c, err)
		return
	}
	if len(items) >= assets.MaxVisualAssetsPerTenant {
		abortWith(c, http.StatusConflict, ErrCodeConflict, "视觉素材库已满，请先整理素材；未发起生图", nil)
		return
	}
	root, err := filepath.Abs(filepath.Join(s.deps.Config.Media.WorkDir, "image-staging"))
	if err == nil {
		err = os.MkdirAll(root, 0o700)
	}
	if err != nil {
		mapError(c, err)
		return
	}
	dir, err := os.MkdirTemp(root, "request-")
	if err != nil {
		mapError(c, err)
		return
	}
	defer os.RemoveAll(dir)
	resp, err := s.deps.AI.GenerateIllustration(c.Request.Context(), &pb.GenerateIllustrationRequest{
		ProviderId: req.ProviderID, Prompt: req.Prompt, Size: req.Size, FyStyleId: req.FyStyleID, OutputDir: dir,
	})
	if err != nil {
		abortWith(c, http.StatusBadGateway, ErrCodeUpstream, "生图未完成；上游可能已计费，不会自动重试", err)
		return
	}
	path, err := filepath.Abs(resp.ImagePath)
	expected, expectedErr := filepath.EvalSymlinks(filepath.Join(dir, "illustration.png"))
	actual, actualErr := filepath.EvalSymlinks(path)
	if err != nil || expectedErr != nil || actualErr != nil || actual != expected || filepath.Dir(expected) != dir || resp.ProviderId != req.ProviderID {
		abortWith(c, http.StatusBadGateway, ErrCodeUpstream, "生成素材未通过来源校验", nil)
		return
	}
	asset, ext, err := inspectVisual(path, "illustration.png")
	if err != nil {
		abortWith(c, http.StatusBadGateway, ErrCodeUpstream, err.Error(), nil)
		return
	}
	asset.Role, asset.Description, asset.Subject, asset.Pose = req.Role, strings.TrimSpace(req.Description), req.Subject, req.Pose
	asset.Generated = &assets.VisualGeneration{Provider: resp.ProviderId, Model: resp.Model,
		FyStyleID: resp.FyStyleId, FyStyleSHA256: resp.FyStyleSha256}
	asset, _, err = store.SaveVisual(tenantOf(c), path, ext, asset)
	if err != nil {
		mapError(c, err)
		return
	}
	respondOK(c, visualUploadResponse{VisualAsset: asset, PreviewURL: "/api/v1/assets/visuals/" + asset.AssetID})
}
