package archive

import (
	"fmt"
	"log/slog"
	"strings"
)

// Options 是归档配置（与 config.ArchiveConfig 字段一一对应）。
type Options struct {
	// Backend 取值 none / local / s3。
	Backend string
	// LocalDir 是 local 后端的根目录。
	LocalDir string
	// KeepAll 为 true 时不做任何本地清理。
	KeepAll bool
	// KeepNormalized 保留归一化中间产物。
	KeepNormalized bool

	S3 S3Options
}

// New 依据配置构造归档器。
//
// 未知的 backend 取值**必须报错**而不是静默退化为 none：
// 把 `s3` 拼成 `S3 ` 或 `s4` 却什么都不归档，
// 会在磁盘写满的那一天才被发现。
//
// 另外，s3 后端配置不完整时报错而不是回退到 local：
// 那属于部署配置错误，静默改变产物去向比直接失败危险得多。
func New(opt Options, logger *slog.Logger) (Archiver, error) {
	if logger == nil {
		logger = slog.Default()
	}

	switch strings.ToLower(strings.TrimSpace(opt.Backend)) {
	case "", "none":
		return NoopArchiver{}, nil

	case "local":
		return NewLocal(opt.LocalDir)

	case "s3", "minio":
		a, err := NewS3(opt.S3, logger)
		if err != nil {
			return nil, err
		}
		logger.Info("归档后端已启用",
			"backend", "s3",
			"endpoint", opt.S3.Endpoint,
			"bucket", opt.S3.Bucket,
			"secure", opt.S3.UseSSL,
		)
		return a, nil

	default:
		return nil, fmt.Errorf(
			"archive: 未知的后端 %q（可选：none / local / s3）", opt.Backend)
	}
}

// CleanupOptions 由归档配置推导。
func (o Options) CleanupOptions() CleanupOptions {
	return CleanupOptions{
		KeepAll:        o.KeepAll,
		KeepNormalized: o.KeepNormalized,
	}
}
