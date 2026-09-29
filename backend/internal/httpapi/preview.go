package httpapi

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/itJinYu/SciDirector/backend/internal/logging"
	"github.com/itJinYu/SciDirector/backend/internal/media"
)

// EffectsPreviewer 是"渲染一段效果预览"的能力（由 media.Runner 实现）。
//
// 与 AudioProber 同样的做法：接口定义在**使用方**，httpapi 只依赖一个方法签名。
// 为 nil 时预览接口返回 501，能力缺失不该让网关起不来。
type EffectsPreviewer interface {
	RenderEffectsPreview(ctx context.Context, workDir, out string, spec media.PreviewSpec) error
}

// 预览的时长上限。预览是用来"快速看一眼"的，不是第二个成片接口。
const (
	maxPreviewDurationSec = 15.0
	// previewMaxAge 之后的历史预览会被清理。
	//
	// 预览是**一次性**产物，每次点击都会生成一个。不清理的话它会稳定地
	// 把磁盘吃掉，而这类"跑着跑着磁盘满了"的问题极难与功能本身联系起来。
	previewMaxAge = 2 * time.Hour
)

// previewIDRe 校验预览 id 的形式。
//
// id 会直接参与拼路径，因此它必须**只能**是我们自己签发的那种形式 ——
// 这是路径穿越的第一道闸门。
var previewIDRe = regexp.MustCompile(`^pv[0-9a-f]{16,64}$`)

// previewDir 返回预览产物目录（由已有的媒体工作目录派生，不新增配置项）。
func (s *Server) previewDir() (string, error) {
	workDir := strings.TrimSpace(s.deps.Config.Media.WorkDir)
	if workDir == "" {
		return "", fmt.Errorf("httpapi: 未配置媒体工作目录，无法确定预览目录")
	}
	return filepath.Join(workDir, "previews"), nil
}

// HandlePreviewEffects 渲染一段效果预览并返回它的地址。
//
// 这条路径**刻意复用成片的全部代码**（背景、调色、淡入淡出、字幕样式、配乐），
// 因此预览不可能与成片不一致。另写一套"轻量预览"看起来更省事，
// 但它必然慢慢长歪，而"预览好看、成片不同"比没有预览更糟 ——
// 用户会照着一个骗人的预览去调参数。
func (s *Server) HandlePreviewEffects(c *gin.Context) {
	if s.deps.Previewer == nil {
		abortWith(c, http.StatusNotImplemented, ErrCodeInternal,
			"本部署未启用效果预览（未配置媒体渲染能力）", nil)
		return
	}
	var req PreviewRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, "请求参数非法", err)
		return
	}

	// 与提交任务走同一套校验：预览若接受了任务不接受的东西，
	// 用户会以为"预览能用、提交却报错"是 bug。
	if err := req.Effects.Validate(); err != nil {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, "effects 参数非法", err)
		return
	}
	if _, err := media.PlanGrade(req.Effects.Grade, req.Effects.GradeStrength); err != nil {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, "调色方案非法", err)
		return
	}

	ctx := c.Request.Context()
	tenant := tenantOf(c)

	// 配乐同样要解析 asset_id -> 路径，且请求里带的 path 一律丢弃。
	effects, err := s.resolveEffects(c, tenant, req.Effects)
	if err != nil {
		return // resolveEffects 内部已写过响应
	}

	bgColour, primaryColour, bgStyle, err := s.resolvePreviewPalette(req.StyleGuide)
	if err != nil {
		abortWith(c, http.StatusBadRequest, ErrCodeBadRequest, "style_guide 参数非法", err)
		return
	}

	duration := req.DurationSec
	if duration <= 0 {
		duration = media.DefaultPreviewDurationSec
	}
	if duration > maxPreviewDurationSec {
		duration = maxPreviewDurationSec
	}

	id, err := newPreviewID()
	if err != nil {
		mapError(c, err)
		return
	}
	dir, err := s.previewDir()
	if err != nil {
		mapError(c, err)
		return
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		mapError(c, fmt.Errorf("httpapi: 创建预览目录失败: %w", err))
		return
	}
	s.sweepPreviews(dir)

	out := filepath.Join(dir, id+".mp4")
	spec := media.PreviewSpec{
		BackgroundStyle: bgStyle,
		BackgroundColor: bgColour,
		PrimaryColor:    primaryColour,
		Width:           s.deps.Config.Media.Width,
		Height:          s.deps.Config.Media.Height,
		FPS:             s.deps.Config.Media.FPS,
		DurationSec:     duration,
		Post: media.PostOptions{
			Grade:       media.GradeSpec{Name: effects.Grade, Strength: effects.GradeStrength},
			Fade:        media.FadeSpec{InSec: effects.FadeInSec, OutSec: effects.FadeOutSec},
			FrameHeight: s.deps.Config.Media.Height,
		},
		LoudnessLUFS: effects.LoudnessLUFS,
	}
	if s := effects.SubtitleStyle; s != nil {
		spec.Post.SubtitleStyle = media.SubtitleStyle{
			FontSize:     s.FontSize,
			PrimaryColor: s.PrimaryColor,
			OutlineWidth: s.OutlineWidth,
			MarginV:      s.MarginV,
		}
	}
	if effects.HasBGM() {
		spec.BGM = &media.BgmSpec{
			Path:       effects.BGM.Path,
			VolumeDB:   effects.BGM.VolumeDB,
			Loop:       effects.BGM.LoopOrDefault(),
			FadeInSec:  effects.BGM.FadeInSec,
			FadeOutSec: effects.BGM.FadeOutSec,
		}
	}

	if rerr := s.deps.Previewer.RenderEffectsPreview(ctx, filepath.Join(dir, "work_"+id), out, spec); rerr != nil {
		// 预览失败是 4xx/5xx？这里判 500：参数已经校验过，剩下的失败
		// （ffmpeg 挂了、磁盘满了）属于服务端问题。
		abortWith(c, http.StatusInternalServerError, ErrCodeInternal, "渲染预览失败", rerr)
		return
	}
	logging.FromContext(ctx).Info("效果预览已渲染",
		"preview_id", id, "duration_sec", duration, "background", bgStyle)

	respondOK(c, PreviewResponse{
		PreviewID:   id,
		URL:         "/api/v1/previews/" + id,
		DurationSec: duration,
		Width:       spec.Width,
		Height:      spec.Height,
	})
}

// resolvePreviewPalette 解析预览要用的配色与背景样式。
//
// 配色优先级：请求里显式给的 > 风格预设的 > 缺省。
// 这条与 Python 侧展开预设时的规则**必须一致**，否则预览与成片会用不同的颜色。
func (s *Server) resolvePreviewPalette(styleGuide map[string]any) (bg, primary, style string, err error) {
	presetID := stringField(styleGuide, "preset")
	preset, ok := media.LookupStylePreset(presetID)
	if !ok {
		return "", "", "", fmt.Errorf("未知的风格预设 %q", presetID)
	}
	bg = preset.BackgroundColor
	primary = preset.PrimaryColor
	if v := stringField(styleGuide, "background_color"); v != "" {
		bg = v
	}
	if v := stringField(styleGuide, "primary_color"); v != "" {
		primary = v
	}
	style = stringField(styleGuide, "background_style")
	if !media.IsBackgroundStyle(style) {
		return "", "", "", fmt.Errorf("未知的背景样式 %q；可用：%s",
			style, strings.Join(media.BackgroundStyleIDs(), ", "))
	}
	return bg, primary, style, nil
}

// stringField 从任意 JSON 对象里取一个字符串字段（非字符串返回空串）。
func stringField(m map[string]any, key string) string {
	if m == nil {
		return ""
	}
	v, ok := m[key]
	if !ok {
		return ""
	}
	s, _ := v.(string)
	return strings.TrimSpace(s)
}

// sweepPreviews 清掉过期的历史预览（尽力而为）。
//
// 失败只记日志：清理是**维护性**动作，它自己出错不该让"渲染预览"这个
// 用户主动发起的操作失败。
func (s *Server) sweepPreviews(dir string) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return
	}
	cutoff := time.Now().Add(-previewMaxAge)
	removed := 0
	for _, e := range entries {
		info, ierr := e.Info()
		if ierr != nil || info.IsDir() {
			continue
		}
		if info.ModTime().After(cutoff) {
			continue
		}
		if os.Remove(filepath.Join(dir, e.Name())) == nil {
			removed++
		}
	}
	if removed > 0 {
		logging.FromContext(context.Background()).Info("已清理过期预览", "removed", removed)
	}
}

// HandleGetPreview 把预览产物交给浏览器播放（支持 Range）。
func (s *Server) HandleGetPreview(c *gin.Context) {
	id := c.Param("previewID")
	if !previewIDRe.MatchString(id) {
		// 非法 id 直接 404：它不可能是我们签发的。
		abortWith(c, http.StatusNotFound, ErrCodeNotFound, "预览不存在", nil)
		return
	}
	dir, err := s.previewDir()
	if err != nil {
		mapError(c, err)
		return
	}
	path := filepath.Join(dir, id+".mp4")
	st, err := os.Stat(path)
	if err != nil || st.IsDir() {
		abortWith(c, http.StatusNotFound, ErrCodeNotFound, "预览不存在或已过期（预览仅保留一段时间）", err)
		return
	}
	c.File(path)
}

// newPreviewID 生成一个预览 id（格式见 previewIDRe）。
func newPreviewID() (string, error) {
	buf := make([]byte, 12)
	if _, err := rand.Read(buf); err != nil {
		return "", fmt.Errorf("httpapi: 生成预览 id 失败: %w", err)
	}
	return "pv" + hex.EncodeToString(buf), nil
}
