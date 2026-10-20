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

// HandleRepairEvidence serves a stored comparison by indices, never a client path.
func (s *Server) HandleRepairEvidence(c *gin.Context) {
	job, ok := s.loadJobForTenant(c, c.Param("jobID"))
	if !ok {
		return
	}
	shot := job.FindShot(c.Param("shotID"))
	if shot != nil {
		review, e1 := strconv.Atoi(c.Param("reviewIndex"))
		task, e2 := strconv.Atoi(c.Param("taskIndex"))
		pair, e3 := strconv.Atoi(c.Param("pairIndex"))
		if e1 == nil && e2 == nil && e3 == nil && review >= 0 && review < len(shot.Feedbacks) &&
			task >= 0 && task < len(shot.Feedbacks[review].RepairTasks) {
			pairs := shot.Feedbacks[review].RepairTasks[task].EvidencePairs
			if pair >= 0 && pair < len(pairs) {
				file := ""
				switch c.Param("side") {
				case "before":
					file = pairs[pair].Before
				case "after":
					file = pairs[pair].After
				}
				if info, err := os.Stat(file); err == nil && info.Mode().IsRegular() {
					c.Header("Cache-Control", "private, no-store")
					c.Header("X-Content-Type-Options", "nosniff")
					c.File(file)
					return
				}
			}
		}
	}
	abortWith(c, http.StatusNotFound, ErrCodeNotFound, "修复对照帧不存在或已被清理", nil)
}
