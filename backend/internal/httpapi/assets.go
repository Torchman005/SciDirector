package httpapi

import (
	"context"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/gin-gonic/gin"

	"github.com/itJinYu/SciDirector/backend/internal/assets"
	"github.com/itJinYu/SciDirector/backend/internal/domain"
	"github.com/itJinYu/SciDirector/backend/internal/logging"
	"github.com/itJinYu/SciDirector/backend/internal/media"
)

// 上传限制。数值取的是"够用且不至于被拿来当网盘"的量级：
// 320kbps 的 5 分钟 mp3 约 12 MB，20 MB 留了余量但仍能挡住整轨无损。
const (
	maxAssetBytes       = 20 << 20
	maxAssetDurationSec = 900.0
)

// AudioProber 是"探测一个音频文件"的能力（由 media.Runner 实现）。
//
// 与 Reconciler 同样的做法：接口定义在**使用方**（httpapi）而不是实现方，
// 这样 httpapi 只依赖一个方法签名，不必把整套媒体栈拉进编译期依赖。
// 为 nil 时上传接口返回 501，理由同 Inspector/Metrics —— 能力缺失
// 不该让网关整个起不来。
type AudioProber interface {
	ProbeAudio(ctx context.Context, path string) (float64, error)
	// ProbeAudioLevel 测平均/峰值电平，用于上传后告诉用户"这个文件多响"。
	ProbeAudioLevel(ctx context.Context, path string) (media.AudioLevel, error)
}

// assetIDRe 校验素材 id 的形式（会参与拼路径，因此必须严格）。
var assetIDRe = regexp.MustCompile(`^[0-9a-f]{16,64}$`)

// HandleGetAsset 把上传的素材原样交回浏览器，用于**试听**。
//
// 走 c.File（内部是 http.ServeFile）而不是自己读文件：它自带 Range 支持，
// 浏览器才能拖动音频进度条。
//
// 这里必须防目录穿越：id 直接参与拼路径，因此先用正则卡死形式 ——
// 素材 id 是服务端签发的十六进制串，任何别的东西都不该匹配上。
func (s *Server) HandleGetAsset(c *gin.Context) {
	id := c.Param("assetID")
	if !assetIDRe.MatchString(id) {
		// 非法 id 直接 404：它不可能是我们签发的。
		abortWith(c, http.StatusNotFound, ErrCodeNotFound, "素材不存在", nil)
		return
	}
	store, err := s.assetStore()
	if err != nil {
		mapError(c, err)
		return
	}
	// Resolve 只接受合法 id，并且只在**该租户自己的目录**里找。
	path, err := store.Resolve(tenantOf(c), id)
	if err != nil {
		abortWith(c, http.StatusNotFound, ErrCodeNotFound, "素材不存在或已被清理", err)
		return
	}
	st, serr := os.Stat(path)
	if serr != nil || st.IsDir() {
		abortWith(c, http.StatusNotFound, ErrCodeNotFound, "素材不存在或已被清理", serr)
		return
	}
	c.File(path)
}

// assetStore 按需构造素材库。
//
// 目录由**已有的**媒体工作目录派生，不引入新配置：多一个配置项就多一处
// 「部署时忘了配」的机会，而这里没有按环境变化的理由。
func (s *Server) assetStore() (*assets.Store, error) {
	workDir := strings.TrimSpace(s.deps.Config.Media.WorkDir)
	if workDir == "" {
		return nil, fmt.Errorf("httpapi: 未配置媒体工作目录，无法确定素材目录")
	}
	return assets.NewStore(filepath.Join(workDir, "assets"))
}

// HandleUploadAsset 接收一个音频素材（目前只用于背景音乐）。
//
// 两件事必须做对，而它们各自都有更容易写错的版本：
//
//  1. **按内容判定，而不是按扩展名**。扩展名只用来决定存成什么名字，
//     "这是不是音频"一律由 ffprobe 真探一次决定 —— 改个后缀就能绕过检查
//     是最经典的一类自欺。
//  2. **请求方给的字符串不参与拼路径**。文件名完全由服务端生成
//     （随机 id + 白名单扩展名），客户端文件名只作为展示信息回显。
func (s *Server) HandleUploadAsset(c *gin.Context) {
	if s.deps.AssetProber == nil {
		abortWith(c, http.StatusNotImplemented, ErrCodeInternal,
			"本部署未启用素材上传（未配置媒体探测能力）", nil)
		return
	}
	store, err := s.assetStore()
	if err != nil {
		mapError(c, err)
		return
	}

	header, err := c.FormFile("file")
	if err != nil {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest,
			`缺少上传文件（multipart 字段名应为 "file"）`, err)
		return
	}
	if header.Size <= 0 {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, "上传文件为空", nil)
		return
	}
	if header.Size > maxAssetBytes {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest,
			fmt.Sprintf("文件过大（上限 %d MB）", maxAssetBytes>>20), nil)
		return
	}

	ext := strings.ToLower(filepath.Ext(header.Filename))
	if !assets.AllowedExt(ext) {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest,
			fmt.Sprintf("不支持的音频格式 %q；支持：%s", ext, strings.Join(assets.AllowedExts(), " ")), nil)
		return
	}

	ctx := c.Request.Context()
	lg := logging.FromContext(ctx)

	// 先落临时文件：ffprobe 必须在文件上跑，而校验不通过时不该在素材库里
	// 留下半成品。扩展名沿用原值，只是为了让 ffprobe 的容器猜测更准。
	tmp, err := os.CreateTemp("", "scid-asset-*"+ext)
	if err != nil {
		mapError(c, fmt.Errorf("httpapi: 创建临时文件失败: %w", err))
		return
	}
	tmpPath := tmp.Name()
	defer os.Remove(tmpPath)

	if err := writeUploaded(tmp, header); err != nil {
		mapError(c, fmt.Errorf("httpapi: 写入上传文件失败: %w", err))
		return
	}
	if err := tmp.Close(); err != nil {
		mapError(c, fmt.Errorf("httpapi: 关闭临时文件失败: %w", err))
		return
	}

	duration, err := s.deps.AssetProber.ProbeAudio(ctx, tmpPath)
	if err != nil {
		// 这里**刻意**把 ffprobe 的失败当成"不是音频"，而不是 500：
		// 对调用方来说，"你传的文件不是音频"是一个明确的 4xx，
		// 而真正属于服务端的故障（磁盘满、ffprobe 缺失）已在别处表现为 5xx。
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest,
			"上传的内容不是可解析的音频文件", err)
		return
	}
	if duration <= 0 {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, "音频时长读取为 0，文件可能已损坏", nil)
		return
	}
	if duration > maxAssetDurationSec {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest,
			fmt.Sprintf("音频过长（%.0f 秒，上限 %.0f 秒）", duration, maxAssetDurationSec), nil)
		return
	}

	tenant := tenantOf(c)
	id, dst, err := store.Save(tenant, tmpPath, ext)
	if err != nil {
		mapError(c, err)
		return
	}

	// 顺带测出这个文件多响。
	//
	// **必须探 dst 而不是 tmpPath**：Save 已经把临时文件 Rename 走了，
	// 再去探原路径会得到"文件不存在" —— 而它表现为"电平字段莫名缺席"，
	// 不是报错，所以第一次很容易漏掉。
	//
	// **尽力而为**：测不到就不给数字（字段为 null），而不是因此拒绝上传 ——
	// 电平是"帮用户判断"的辅助信息，不该成为上传的门槛。
	resp := UploadAssetResponse{
		AssetID:     id,
		Kind:        "audio",
		Filename:    filepath.Base(header.Filename),
		DurationSec: duration,
		SizeBytes:   header.Size,
	}
	if lv, lerr := s.deps.AssetProber.ProbeAudioLevel(ctx, dst); lerr == nil {
		resp.MeanVolumeDBFS = &lv.MeanDBFS
		resp.PeakVolumeDBFS = &lv.PeakDBFS
		// 峰值贴近满刻度 = 源文件很可能已经削顶。那是**源文件的问题**，
		// 调音量救不回来，所以要单独提示，而不是让用户白折腾滑块。
		resp.PeakWarning = lv.PeakDBFS > -1.0
	} else {
		lg.Warn("素材电平探测失败，上传响应不含电平", "error", lerr.Error())
	}
	lg.Info("素材已上传", "asset_id", id, "kind", "audio",
		"duration_sec", duration, "size_bytes", header.Size)

	// 用 respondOK 而不是直接 c.JSON：所有成功响应都必须带 {ok,data} 信封。
	// 这一条不是洁癖 —— 前端的 request() 统一按信封拆包，漏掉信封会让
	// 调用方拿到 undefined 而**不报错**（本项目已经踩过一次，整个页面静默空白）。
	respondOK(c, resp)
}

// resolveEffects 把请求里的 effects 变成可以落库的形态，并在失败时写好响应。
//
// 这里有一处**安全要点**：请求里带的 `bgm.path` 一律丢弃。
// Path 只能由服务端根据 asset_id 解析出来 —— 否则任何人写一个
// `"path": "C:/Windows/..."` 就等于拿到一个"读服务端任意文件"的接口，
// 而且它还会被合成阶段真的打开。
//
// 返回的 error 只在"已经写过响应"时非 nil，调用方直接 return 即可。
func (s *Server) resolveEffects(
	c *gin.Context, tenant string, in domain.Effects,
) (domain.Effects, error) {
	out := in
	if out.Presenter != nil {
		copy := *out.Presenter
		out.Presenter = &copy
		out.Presenter.ModelPath = ""
		if err := out.Presenter.Validate(); err != nil {
			abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, err.Error(), nil)
			return out, err
		}
		store, err := s.assetStore()
		if err != nil {
			mapError(c, err)
			return out, err
		}
		model, err := store.ResolveLive2D(tenant, out.Presenter.AssetID)
		if err != nil {
			abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, "Live2D 模型不存在，请重新导入", nil)
			return out, err
		}
		out.Presenter.ModelPath = model
	}
	if out.BGM == nil {
		return out, nil
	}

	// 无条件清空：即便请求里填了 path，也不认。
	out.BGM.Path = ""

	id := strings.TrimSpace(out.BGM.AssetID)
	if id == "" {
		// 只填了音量/淡入淡出却没给素材：视为没配 BGM，而不是让合成去开空路径。
		out.BGM = nil
		return out, nil
	}

	store, err := s.assetStore()
	if err != nil {
		mapError(c, err)
		return out, err
	}
	path, err := store.Resolve(tenant, id)
	if err != nil {
		// 在**创建任务之前**解析：任务一旦落库就会进队列，等到合成阶段才发现
		// 素材不存在，用户已经白等了几分钟渲染。
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest,
			"背景音乐素材不存在或已被清理，请重新上传", err)
		return out, err
	}
	out.BGM.Path = path
	return out, nil
}

// writeUploaded 把 multipart 里的文件内容写进已打开的临时文件。
func writeUploaded(dst *os.File, header *multipart.FileHeader) error {
	src, err := header.Open()
	if err != nil {
		return err
	}
	defer src.Close()
	// io.Copy 而不是一次性 ReadAll：20MB 的上限虽然不高，但"先读进内存"
	// 这种写法一旦有人把上限调大就会变成内存放大攻击。
	_, err = io.Copy(dst, io.LimitReader(src, maxAssetBytes+1))
	return err
}

// HandleGetArtifact 把成片交给浏览器播放。
//
// 走 `c.File`（内部就是 http.ServeFile）而不是自己读文件：它会自动处理
// **Range 请求**、Last-Modified 与 Content-Type。视频播放在浏览器里依赖
// Range —— 没有它，播放器只能从头下载完才能拖动进度条。
func (s *Server) HandleGetArtifact(c *gin.Context) {
	job, ok := s.loadJobForTenant(c, c.Param("jobID"))
	if !ok {
		return
	}
	path := strings.TrimSpace(job.FinalVideoPath)
	if path == "" {
		abortWith(c, http.StatusNotFound, ErrCodeNotFound,
			"该任务还没有成片（可能仍在合成，或镜头未通过审查）", nil)
		return
	}
	// 路径由 worker 在媒体工作目录里生成，不是请求方给的；这里仍确认它
	// 确实是一个普通文件 —— 否则 c.File 会把目录列表之类的东西也吐出去。
	st, err := os.Stat(path)
	if err != nil || st.IsDir() {
		abortWith(c, http.StatusNotFound, ErrCodeNotFound, "成片文件不存在（可能已被清理）", err)
		return
	}
	c.File(path)
}
