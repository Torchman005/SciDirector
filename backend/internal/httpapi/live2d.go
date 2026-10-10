package httpapi

import (
	"github.com/gin-gonic/gin"
	"github.com/itJinYu/SciDirector/backend/internal/assets"
	"io"
	"net/http"
	"os"
)

// HandleUploadLive2D imports a data-only Cubism archive with bounded extraction.
func (s *Server) HandleUploadLive2D(c *gin.Context) {
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, assets.MaxModelZipBytes+(1<<20))
	header, err := c.FormFile("file")
	if c.Request.MultipartForm != nil {
		defer c.Request.MultipartForm.RemoveAll()
	}
	if err != nil || header.Size <= 0 || header.Size > assets.MaxModelZipBytes {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, "请上传不超过 100 MB 的 Live2D 模型 ZIP", nil)
		return
	}
	src, err := header.Open()
	if err != nil {
		mapError(c, err)
		return
	}
	defer src.Close()
	tmp, err := os.CreateTemp("", "scid-live2d-*.zip")
	if err != nil {
		mapError(c, err)
		return
	}
	defer os.Remove(tmp.Name())
	n, copyErr := io.Copy(tmp, io.LimitReader(src, assets.MaxModelZipBytes+1))
	closeErr := tmp.Close()
	if copyErr != nil || closeErr != nil || n > assets.MaxModelZipBytes {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, "ZIP 写入失败或大小超限", nil)
		return
	}
	store, err := s.assetStore()
	if err != nil {
		mapError(c, err)
		return
	}
	asset, err := store.ImportLive2D(tenantOf(c), tmp.Name(), header.Filename)
	if err != nil {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, err.Error(), nil)
		return
	}
	respondOK(c, asset)
}
