package httpapi

import (
	"fmt"
	"image"
	_ "image/jpeg"
	_ "image/png"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"unicode/utf8"

	"github.com/gin-gonic/gin"
	"github.com/itJinYu/SciDirector/backend/internal/assets"
)

const (
	maxVisualUploadBytes = 2 << 20
	maxVisualDimension   = 4096
	maxVisualPixels      = 12_000_000
	maxSceneAssets       = 6
	maxSceneAssetsBytes  = 8 << 20
)

type visualUploadResponse struct {
	assets.VisualAsset
	PreviewURL string `json:"preview_url"`
}

func (s *Server) HandleUploadVisual(c *gin.Context) {
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, maxVisualUploadBytes+(64<<10))
	header, err := c.FormFile("file")
	if err != nil {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, "请选择 PNG 或 JPEG 图片", err)
		return
	}
	if header.Size <= 0 || header.Size > maxVisualUploadBytes {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, "图片为空或超过 2 MB", nil)
		return
	}
	role := strings.TrimSpace(c.PostForm("role"))
	if role == "" {
		role = "prop"
	}
	if role != "character" && role != "prop" && role != "background" {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, "素材用途必须为角色、道具或背景", nil)
		return
	}
	description := strings.TrimSpace(c.PostForm("description"))
	if utf8.RuneCountInString(description) > 240 {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, "素材说明最多 240 字", nil)
		return
	}
	subject := strings.TrimSpace(c.PostForm("subject"))
	pose := strings.TrimSpace(c.PostForm("pose"))
	if utf8.RuneCountInString(subject) > 48 || utf8.RuneCountInString(pose) > 32 {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, "对象名称最多 48 字，姿态/状态最多 32 字", nil)
		return
	}
	if pose != "" && subject == "" {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, "填写姿态/状态时需填写同一对象名称", nil)
		return
	}
	ext := strings.ToLower(filepath.Ext(header.Filename))
	if ext != ".png" && ext != ".jpg" && ext != ".jpeg" {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, "只支持 PNG 和 JPEG 图片", nil)
		return
	}

	store, err := s.assetStore()
	if err != nil {
		mapError(c, err)
		return
	}
	tmp, err := os.CreateTemp("", "scid-visual-*")
	if err != nil {
		mapError(c, fmt.Errorf("创建图片临时文件失败: %w", err))
		return
	}
	tmpPath := tmp.Name()
	defer os.Remove(tmpPath)
	src, err := header.Open()
	if err == nil {
		_, err = io.Copy(tmp, io.LimitReader(src, maxVisualUploadBytes+1))
		_ = src.Close()
	}
	if err != nil {
		_ = tmp.Close()
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, "读取图片失败", err)
		return
	}
	if err := tmp.Close(); err != nil {
		mapError(c, fmt.Errorf("关闭图片临时文件失败: %w", err))
		return
	}
	info, err := os.Stat(tmpPath)
	if err != nil || info.Size() <= 0 || info.Size() > maxVisualUploadBytes {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, "图片为空或超过 2 MB", err)
		return
	}
	file, err := os.Open(tmpPath)
	if err != nil {
		mapError(c, fmt.Errorf("打开图片临时文件失败: %w", err))
		return
	}
	config, format, decodeErr := image.DecodeConfig(file)
	_ = file.Close()
	if decodeErr != nil || (format != "png" && format != "jpeg") {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, "文件内容不是有效的 PNG/JPEG 图片", decodeErr)
		return
	}
	if (format == "png" && ext != ".png") || (format == "jpeg" && ext != ".jpg" && ext != ".jpeg") {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, "图片内容与文件扩展名不匹配", nil)
		return
	}
	if config.Width < 1 || config.Height < 1 || config.Width > maxVisualDimension || config.Height > maxVisualDimension || int64(config.Width)*int64(config.Height) > maxVisualPixels {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, "图片尺寸超过 4096 × 4096 或 1200 万像素限制", nil)
		return
	}
	// Decode the complete bounded image so a valid header with corrupt pixel data
	// cannot become a broken asset that fails only during video rendering.
	file, err = os.Open(tmpPath)
	if err == nil {
		_, _, err = image.Decode(file)
		_ = file.Close()
	}
	if err != nil {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, "图片像素数据损坏或无法解码", err)
		return
	}
	mimeType, storedExt := "image/jpeg", ".jpg"
	if format == "png" {
		mimeType, storedExt = "image/png", ".png"
	}
	name := filepath.Base(header.Filename)
	if utf8.RuneCountInString(name) > 180 {
		name = string([]rune(name)[:180])
	}
	asset, _, err := store.SaveVisual(tenantOf(c), tmpPath, storedExt, assets.VisualAsset{
		Filename: name, MIMEType: mimeType, Role: role, Description: description, Subject: subject, Pose: pose,
		Width: config.Width, Height: config.Height, SizeBytes: info.Size(),
	})
	if err != nil {
		if strings.Contains(err.Error(), "素材库已满") {
			abortWith(c, http.StatusConflict, ErrCodeConflict, err.Error(), nil)
		} else {
			mapError(c, err)
		}
		return
	}
	respondOK(c, visualUploadResponse{VisualAsset: asset, PreviewURL: "/api/v1/assets/visuals/" + asset.AssetID})
}

func (s *Server) HandleListVisuals(c *gin.Context) {
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
	respondOK(c, items)
}

func (s *Server) HandleGetVisual(c *gin.Context) {
	id := c.Param("id")
	if !assetIDRe.MatchString(id) {
		abortWith(c, http.StatusNotFound, ErrCodeNotFound, "视觉素材不存在", nil)
		return
	}
	store, err := s.assetStore()
	if err != nil {
		mapError(c, err)
		return
	}
	asset, path, err := store.ResolveVisual(tenantOf(c), id)
	if err != nil {
		abortWith(c, http.StatusNotFound, ErrCodeNotFound, "视觉素材不存在或已被清理", err)
		return
	}
	c.Header("Content-Type", asset.MIMEType)
	c.Header("X-Content-Type-Options", "nosniff")
	c.Header("Cache-Control", "private, no-store")
	c.File(path)
}

// resolveVisualSelections replaces client-supplied descriptions with metadata
// from the tenant-owned import manifest. The generation request only accepts
// IDs, never URLs, file paths, or caller-authored image payloads.
func (s *Server) resolveVisualSelections(tenant string, guide map[string]any) error {
	if guide == nil {
		return nil
	}
	if _, exists := guide["scene_asset_sources"]; exists {
		return fmt.Errorf("scene_asset_sources 是服务端字段，不能由请求方设置")
	}
	raw, exists := guide["scene_assets"]
	if !exists || raw == nil {
		return nil
	}
	items, ok := raw.([]any)
	if !ok || len(items) > maxSceneAssets {
		return fmt.Errorf("scene_assets 必须为最多 %d 张图片的数组", maxSceneAssets)
	}
	store, err := s.assetStore()
	if err != nil {
		return err
	}
	resolved := make([]assets.VisualAsset, 0, len(items))
	seen := make(map[string]struct{}, len(items))
	var total int64
	for _, item := range items {
		entry, ok := item.(map[string]any)
		if !ok || len(entry) != 1 {
			return fmt.Errorf("scene_assets 每项只能包含服务端签发的 asset_id")
		}
		id, ok := entry["asset_id"].(string)
		if !ok || !assetIDRe.MatchString(id) {
			return fmt.Errorf("scene_assets 包含无效的 asset_id")
		}
		if _, exists := seen[id]; exists {
			return fmt.Errorf("scene_assets 不能重复引用同一张图片")
		}
		seen[id] = struct{}{}
		asset, _, err := store.ResolveVisual(tenant, id)
		if err != nil {
			return fmt.Errorf("视觉素材 %s 不存在或不属于当前账号", id)
		}
		if asset.SizeBytes > maxVisualUploadBytes {
			return fmt.Errorf("视觉素材 %s 超过单张大小限制", id)
		}
		total += asset.SizeBytes
		if total > maxSceneAssetsBytes {
			return fmt.Errorf("所选视觉素材总大小不能超过 8 MB")
		}
		resolved = append(resolved, asset)
	}
	guide["scene_assets"] = resolved
	return nil
}
