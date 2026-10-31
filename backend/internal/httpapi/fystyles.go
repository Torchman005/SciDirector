package httpapi

import (
	"encoding/json"
	"fmt"
	"net/http"

	"github.com/gin-gonic/gin"
	pb "github.com/itJinYu/SciDirector/backend/internal/pb/scidirector/v1"
)

// Catalog and source resolution share Python's pinned library reader.
func (s *Server) HandleFyStyles(c *gin.Context) {
	resp, err := s.deps.AI.GetFyStyles(c.Request.Context(), &pb.FyStylesRequest{StyleId: c.Param("id")})
	if err != nil {
		abortWith(c, http.StatusServiceUnavailable, ErrCodeUpstream, "画风库不可用，请检查 AI 服务版本及画风库安装", err)
		return
	}
	raw := resp.CatalogJson
	if c.Param("id") != "" {
		raw = resp.SourceJson
	}
	if !json.Valid([]byte(raw)) {
		abortWith(c, http.StatusBadGateway, ErrCodeUpstream, "画风库响应无效", nil)
		return
	}
	respondOK(c, json.RawMessage(raw))
}

func (s *Server) resolveFyStyle(c *gin.Context, sg map[string]any) error {
	if sg["animation_style"] != "fy" {
		return nil
	}
	resp, err := s.deps.AI.GetFyStyles(c.Request.Context(), &pb.FyStylesRequest{StyleId: sg["fy_style_id"].(string)})
	if err != nil {
		return err
	}
	var source map[string]any
	if err := json.Unmarshal([]byte(resp.SourceJson), &source); err != nil {
		return fmt.Errorf("画风来源无效: %w", err)
	}
	if source["id"] != sg["fy_style_id"] || source["content"] == nil || source["sha256"] == nil {
		return fmt.Errorf("画风来源与所选编号不符")
	}
	sg["fy_style_source"] = source
	// The library owns composition, colors and material. Old defaults must not
	// compete with its source in downstream prompts or repairs.
	for _, key := range []string{"preset", "background_style", "primary_color", "background_color", "theme"} {
		delete(sg, key)
	}
	return nil
}
