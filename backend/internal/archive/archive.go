// Package archive 负责把产物放到长期存储，并按策略清理本地卷。
//
// 设计要点：
//
//  1. **归档与保留策略分离**。前者是"把文件送出去"，后者是"本地能删什么"。
//     两者容易混在一起写，结果是换一个存储后端就要重新审一遍删除逻辑。
//     这里 PlanCleanup 是纯函数，不碰 IO，因此可以被完整单测。
//
//  2. **未配置归档时必须是可用的**。单机开发与 CI 没有 MinIO，
//     此时 NoopArchiver 静默跳过，而**保留策略依然生效** ——
//     否则本地卷会在几次运行后就被抽帧 PNG 撑爆。
//
//  3. **归档失败不阻断业务**。产物已经生成、任务已经成功，
//     因为对象存储抖动就把任务判失败，代价远大于收益。调用方负责降级。
package archive

import (
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

// ObjectKind 是归档对象的类别，决定保留策略。
type ObjectKind string

const (
	// KindFinal 是最终成片：本地必须保留一份，供播放与回放。
	KindFinal ObjectKind = "final"
	// KindSubtitle 是字幕文件：极小，一并保留。
	KindSubtitle ObjectKind = "subtitle"
	// KindShot 是单镜头片段：HITL 重做时要用，删了就得重渲，因此保留。
	KindShot ObjectKind = "shot"
	// KindNormalized 是归一化中间产物：纯中间态，可删。
	KindNormalized ObjectKind = "normalized"
	// KindFrame 是抽帧 PNG：体积大且可随时重建，优先删。
	KindFrame ObjectKind = "frame"
)

// Archiver 把本地文件送入长期存储，返回可对外暴露的 URI。
type Archiver interface {
	// Put 上传/复制一个文件。key 由 ObjectKey 生成。
	Put(ctx context.Context, localPath, key string) (uri string, err error)
	// Enabled 表示归档是否真的会发生。false 时调用方可以跳过整个流程。
	Enabled() bool
	// Kind 返回后端名称，用于日志与事件留痕。
	Kind() string
}

// ObjectKey 生成对象键。
//
// 形如 `jobs/<jobID>/<kind>/<filename>`。
// 用 jobID 作为第一级目录而不是日期：排查某个任务时可以直接列出前缀，
// 而按日期分片虽然对冷存储更友好，却让"这个任务的产物在哪"变成多次前缀扫描。
//
// 必须清洗 filename：它可能来自渲染器，含路径分隔符或 `..`，
// 拼进对象键会造成越界写入（对象存储的键没有目录概念，但服务端策略会按前缀授权）。
func ObjectKey(jobID string, kind ObjectKind, filename string) string {
	safe := sanitizeSegment(filename)
	return fmt.Sprintf("jobs/%s/%s/%s", sanitizeSegment(jobID), kind, safe)
}

// sanitizeSegment 把一段文本清洗成安全的路径片段。
func sanitizeSegment(s string) string {
	s = strings.ReplaceAll(s, "\\", "/")
	// 只取最后一段，丢弃任何目录成分。
	if i := strings.LastIndex(s, "/"); i >= 0 {
		s = s[i+1:]
	}
	s = strings.TrimSpace(s)
	if s == "" || s == "." || s == ".." {
		return "unnamed"
	}
	// 替换掉可能干扰键解析的字符。
	replacer := strings.NewReplacer(" ", "_", "?", "_", "#", "_", "%", "_")
	return replacer.Replace(s)
}

// ---------------------------------------------------------------------------
// Noop
// ---------------------------------------------------------------------------

// NoopArchiver 不做任何归档。
//
// 它存在的意义是让「没有配置对象存储」成为一条**正常路径**而不是错误路径：
// 单机开发、CI、离线演示都走这里，业务代码无需到处判空。
type NoopArchiver struct{}

func (NoopArchiver) Put(context.Context, string, string) (string, error) { return "", nil }
func (NoopArchiver) Enabled() bool                                       { return false }
func (NoopArchiver) Kind() string                                        { return "none" }

// ---------------------------------------------------------------------------
// 本地目录
// ---------------------------------------------------------------------------

// LocalArchiver 把文件复制到本地归档目录。
//
// 用于单机部署与测试：语义与对象存储一致（同一个 key 对应同一个目标），
// 因此上层代码不必为「有没有对象存储」分叉。
type LocalArchiver struct {
	root string
}

// NewLocal 创建本地归档器。root 为空时返回错误 ——
// 那说明配置写错了，静默退化成一个"什么都没归档"的实现更难排查。
func NewLocal(root string) (*LocalArchiver, error) {
	if strings.TrimSpace(root) == "" {
		return nil, fmt.Errorf("archive: 本地归档目录不能为空")
	}
	abs, err := filepath.Abs(root)
	if err != nil {
		return nil, fmt.Errorf("archive: 解析归档目录失败: %w", err)
	}
	return &LocalArchiver{root: abs}, nil
}

func (a *LocalArchiver) Enabled() bool { return true }
func (a *LocalArchiver) Kind() string  { return "local" }

// Put 把文件复制到 <root>/<key>。
func (a *LocalArchiver) Put(ctx context.Context, localPath, key string) (string, error) {
	if err := ctx.Err(); err != nil {
		return "", err
	}
	dst := filepath.Join(a.root, filepath.FromSlash(key))
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return "", fmt.Errorf("archive: 创建归档目录失败: %w", err)
	}
	if err := copyFile(localPath, dst); err != nil {
		return "", err
	}
	return "file://" + filepath.ToSlash(dst), nil
}

// copyFile 复制文件并保留权限位。
func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return fmt.Errorf("archive: 打开源文件失败 %s: %w", src, err)
	}
	defer func() { _ = in.Close() }()

	info, err := in.Stat()
	if err != nil {
		return fmt.Errorf("archive: 读取源文件信息失败 %s: %w", src, err)
	}
	if info.IsDir() {
		return fmt.Errorf("archive: 源路径是目录而非文件: %s", src)
	}

	out, err := os.OpenFile(dst, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, info.Mode().Perm())
	if err != nil {
		return fmt.Errorf("archive: 创建目标文件失败 %s: %w", dst, err)
	}
	if _, err := io.Copy(out, in); err != nil {
		_ = out.Close()
		return fmt.Errorf("archive: 复制失败 %s -> %s: %w", src, dst, err)
	}
	if err := out.Close(); err != nil {
		return fmt.Errorf("archive: 关闭目标文件失败 %s: %w", dst, err)
	}
	return nil
}
