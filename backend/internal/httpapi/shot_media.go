package httpapi

import (
	"net/http"
	"os"
	"strconv"

	"github.com/gin-gonic/gin"
)

// HandleShotMedia serves only the current server-owned artifact. No request path
// is interpreted as a filesystem path, and ownership is checked before indices.
func (s *Server) HandleShotMedia(c *gin.Context) {
	job, ok := s.loadJobForTenant(c, c.Param("jobID"))
	if !ok {
		return
	}
	for _, shot := range job.Shots {
		if shot.ShotID != c.Param("shotID") {
			continue
		}
		a := shot.Artifact
		if a == nil {
			break
		}
		if version := c.Query("version"); version != "" && version != a.ArtifactID {
			abortWith(c, http.StatusConflict, ErrCodeConflict, "镜头已更新，请刷新后查看新版本", nil)
			return
		}
		file := a.VideoPath
		if raw := c.Param("frameIndex"); raw != "" {
			i, err := strconv.Atoi(raw)
			if err != nil || i < 0 || i >= len(a.FrameSamples) {
				break
			}
			file = a.FrameSamples[i]
		}
		info, err := os.Stat(file)
		if err != nil || !info.Mode().IsRegular() {
			break
		}
		c.Header("Cache-Control", "private, no-store")
		c.Header("X-Content-Type-Options", "nosniff")
		c.File(file) // ServeFile implements Range for seekable video playback.
		return
	}
	abortWith(c, http.StatusNotFound, ErrCodeNotFound, "镜头产物尚未生成或已被清理", nil)
}
