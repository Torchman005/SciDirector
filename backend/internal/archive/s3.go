package archive

import (
	"context"
	"fmt"
	"log/slog"
	"path/filepath"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

// S3Options 是对象存储（MinIO / S3 兼容）的连接参数。
type S3Options struct {
	Endpoint  string
	AccessKey string
	SecretKey string
	Bucket    string
	UseSSL    bool
	Region    string
}

// S3Archiver 把产物上传到 S3 兼容对象存储。
//
// # 关于本实现的验证状态
//
// **已对真实 S3 API 完成端到端验证**（`s3_e2e_test.go`，用 `make test-s3` 运行）：
// 按需建桶、上传、用**独立客户端**读回并逐字节比对、Content-Type、敌意文件名的
// 键清洗在真实存储里的落点、端点不可达时如实报错；另有
// `worker/archive_test.go::TestFinalizeArtifactsUploadsToRealS3` 把真实归档器
// 接进真实的 `finalizeArtifacts`，验证「本地路径确实能被归档器读到」
// 以及「归档成功后本地清理照常发生」。
//
// 需要如实说明的边界：上述验证跑在 **moto**（一个 S3 API 实现）上，
// 尚未对生产将使用的具体对象存储复跑。用例只依赖 S3 API、不绑定产品，
// 因此换端点重跑即可：
//
//	SCID_TEST_S3_ENDPOINT=... make test-s3
//
// 另外，原先计划使用的 MinIO 开源版**已归档停更**（dl.min.io 已返回 410），
// 生产选型需另定 —— 见 docs/ROADMAP.md 的说明。
type S3Archiver struct {
	client *minio.Client
	bucket string
	logger *slog.Logger
}

// NewS3 构造 S3 归档器。
//
// 构造期只建立客户端，不做网络探测：进程启动不应被对象存储的可用性绑架。
// bucket 的存在性在首次 Put 时按需创建。
func NewS3(opt S3Options, logger *slog.Logger) (*S3Archiver, error) {
	if opt.Endpoint == "" {
		return nil, fmt.Errorf("archive: S3 endpoint 不能为空")
	}
	if opt.Bucket == "" {
		return nil, fmt.Errorf("archive: S3 bucket 不能为空")
	}
	if logger == nil {
		logger = slog.Default()
	}

	client, err := minio.New(opt.Endpoint, &minio.Options{
		Creds:  credentials.NewStaticV4(opt.AccessKey, opt.SecretKey, ""),
		Secure: opt.UseSSL,
		Region: opt.Region,
	})
	if err != nil {
		return nil, fmt.Errorf("archive: 构造 S3 客户端失败: %w", err)
	}
	return &S3Archiver{client: client, bucket: opt.Bucket, logger: logger}, nil
}

func (a *S3Archiver) Enabled() bool { return true }
func (a *S3Archiver) Kind() string  { return "s3" }

// Put 上传文件，必要时创建 bucket。
func (a *S3Archiver) Put(ctx context.Context, localPath, key string) (string, error) {
	if err := a.ensureBucket(ctx); err != nil {
		return "", err
	}

	// 单次上传不设内部超时，由 ctx 统一控制：重试与超时策略应当只有一处。
	_, err := a.client.FPutObject(ctx, a.bucket, key, localPath, minio.PutObjectOptions{
		ContentType: contentTypeFor(key),
	})
	if err != nil {
		return "", fmt.Errorf("archive: 上传 %s 失败: %w", key, err)
	}
	return fmt.Sprintf("s3://%s/%s", a.bucket, key), nil
}

// ensureBucket 按需建桶。
//
// 用幂等语义：并发上传时多个协程可能同时走到这里，
// BucketExists 之后再 MakeBucket 之间的竞态是正常的，
// 此时把"已存在"当成成功即可，不必加锁。
func (a *S3Archiver) ensureBucket(ctx context.Context) error {
	exists, err := a.client.BucketExists(ctx, a.bucket)
	if err != nil {
		return fmt.Errorf("archive: 检查 bucket 失败: %w", err)
	}
	if exists {
		return nil
	}
	if err := a.client.MakeBucket(ctx, a.bucket, minio.MakeBucketOptions{}); err != nil {
		// 竞态下另一个协程可能已经建好了：再查一次，存在即视为成功。
		if ok, e := a.client.BucketExists(ctx, a.bucket); e == nil && ok {
			return nil
		}
		return fmt.Errorf("archive: 创建 bucket %s 失败: %w", a.bucket, err)
	}
	a.logger.Info("已创建归档 bucket", "bucket", a.bucket)
	return nil
}

// contentTypeFor 依据扩展名给出 Content-Type。
//
// 不给对类型时，浏览器打开对象会变成下载而不是内联预览 ——
// 审核台点开成片却开始下载，是个很容易被忽略但体验很差的细节。
func contentTypeFor(key string) string {
	switch filepath.Ext(key) {
	case ".mp4":
		return "video/mp4"
	case ".srt":
		return "application/x-subrip"
	case ".json":
		return "application/json"
	case ".png":
		return "image/png"
	case ".txt":
		return "text/plain; charset=utf-8"
	default:
		return "application/octet-stream"
	}
}
