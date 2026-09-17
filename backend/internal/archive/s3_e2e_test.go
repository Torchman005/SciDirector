package archive

// 本文件是 **s3 归档路径的端到端验证**。
//
// 为什么单独成一个文件、并且用环境变量门控：
// `s3.go` 的实现此前只被「构造期不报错」覆盖过（`TestS3ArchiverConstruction`），
// 真刀真枪的 Put / 建桶 / 取回一次都没跑过 —— 文档里那句
// 「s3 路径未经端到端验证」说的就是这件事。要验证它，必须有**真的 S3 端点**：
// 用 mock 替掉 HTTP 层，测的就只是自己写的替身。
//
// 因此这里要求显式给出端点，缺失时**跳过而不是失败**（CI 上没有对象存储不应变红）：
//
//	SCID_TEST_S3_ENDPOINT=http://127.0.0.1:9000 \
//	SCID_TEST_S3_ACCESS_KEY=scidirector \
//	SCID_TEST_S3_SECRET_KEY=scidirector-secret \
//	go test ./internal/archive/ -run TestS3 -v
//
// 每个用例用**独立桶名**（带随机后缀），既能顺带验证「桶不存在时按需创建」，
// 也避免并行跑时互相踩。

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

// requireS3 读取真实端点配置；未配置时跳过并说明缺什么。
func requireS3(t *testing.T) S3Options {
	t.Helper()
	endpoint := strings.TrimSpace(os.Getenv("SCID_TEST_S3_ENDPOINT"))
	if endpoint == "" {
		t.Skip("未设置 SCID_TEST_S3_ENDPOINT，跳过 s3 端到端验证（需要真实的 S3/MinIO 端点）")
	}
	// minio-go 的 Endpoint 不带 scheme，scheme 由 UseSSL 决定。
	useSSL := strings.HasPrefix(endpoint, "https://")
	host := strings.TrimPrefix(strings.TrimPrefix(endpoint, "https://"), "http://")
	host = strings.TrimSuffix(host, "/")

	opt := S3Options{
		Endpoint:  host,
		AccessKey: os.Getenv("SCID_TEST_S3_ACCESS_KEY"),
		SecretKey: os.Getenv("SCID_TEST_S3_SECRET_KEY"),
		Bucket:    uniqueBucket(t),
		UseSSL:    useSSL,
	}
	if opt.AccessKey == "" || opt.SecretKey == "" {
		t.Skip("未设置 SCID_TEST_S3_ACCESS_KEY / SCID_TEST_S3_SECRET_KEY，跳过 s3 端到端验证")
	}
	return opt
}

func uniqueBucket(t *testing.T) string {
	t.Helper()
	buf := make([]byte, 4)
	if _, err := rand.Read(buf); err != nil {
		t.Fatalf("生成随机桶名失败: %v", err)
	}
	// 桶名必须是小写且符合 DNS 规则，因此只用十六进制字符。
	return fmt.Sprintf("scid-e2e-%d-%s", time.Now().Unix()%100000, hex.EncodeToString(buf))
}

// clientFor 用与生产同样的凭据建一个直连客户端，供测试侧独立校验。
//
// 刻意**不**复用 S3Archiver 内部的 client：验证要用一条独立路径去读回来，
// 否则「上传成功」可能只是 archiver 自己的错觉。
func clientFor(t *testing.T, opt S3Options) *minio.Client {
	t.Helper()
	c, err := minio.New(opt.Endpoint, &minio.Options{
		Creds:  credentials.NewStaticV4(opt.AccessKey, opt.SecretKey, ""),
		Secure: opt.UseSSL,
	})
	if err != nil {
		t.Fatalf("构造校验用 S3 客户端失败: %v", err)
	}
	return c
}

// TestS3ArchiverRoundTrip 覆盖主路径：按需建桶 → 上传 → 独立读回比对字节。
//
// 断言到**字节相等**而不是"没报错"：上传是异步分片的，只断言无错误
// 会漏掉「传了个空对象」这类静默失败 —— 而对象存储上的空产物
// 恰恰是最难在事后发现的那种。
func TestS3ArchiverRoundTrip(t *testing.T) {
	opt := requireS3(t)
	ctx := context.Background()

	arch, err := NewS3(opt, nil)
	if err != nil {
		t.Fatalf("构造 S3Archiver 失败: %v", err)
	}

	// 桶此刻还不存在（随机名），因此这一步同时验证了 ensureBucket。
	content := []byte("SciDirector s3 roundtrip 内容 —— 含中文与 emoji 🎬")
	local := filepath.Join(t.TempDir(), "clip.mp4")
	if err := os.WriteFile(local, content, 0o644); err != nil {
		t.Fatalf("写本地文件失败: %v", err)
	}

	key := ObjectKey("job-e2e", KindFinal, "clip.mp4")
	uri, err := arch.Put(ctx, local, key)
	if err != nil {
		t.Fatalf("归档上传失败: %v", err)
	}
	wantURI := fmt.Sprintf("s3://%s/%s", opt.Bucket, key)
	if uri != wantURI {
		t.Errorf("返回 URI 期望 %q，实际 %q", wantURI, uri)
	}

	cli := clientFor(t, opt)

	exists, err := cli.BucketExists(ctx, opt.Bucket)
	if err != nil {
		t.Fatalf("检查桶失败: %v", err)
	}
	if !exists {
		t.Fatal("Put 之后桶仍不存在 —— ensureBucket 没有真正建桶")
	}

	obj, err := cli.GetObject(ctx, opt.Bucket, key, minio.GetObjectOptions{})
	if err != nil {
		t.Fatalf("取回对象失败: %v", err)
	}
	defer func() { _ = obj.Close() }()

	got, err := io.ReadAll(obj)
	if err != nil {
		t.Fatalf("读取对象内容失败: %v", err)
	}
	if !bytes.Equal(got, content) {
		t.Errorf("对象内容与本地不一致：本地 %d 字节，远端 %d 字节", len(content), len(got))
	}

	// Content-Type 决定浏览器是内联播放还是下载 —— 审核台里是体感差异。
	info, err := cli.StatObject(ctx, opt.Bucket, key, minio.StatObjectOptions{})
	if err != nil {
		t.Fatalf("StatObject 失败: %v", err)
	}
	if !strings.HasPrefix(info.ContentType, "video/mp4") {
		t.Errorf("Content-Type 期望 video/mp4，实际 %q —— 点开成片会变成下载", info.ContentType)
	}
	if info.Size != int64(len(content)) {
		t.Errorf("远端大小期望 %d，实际 %d", len(content), info.Size)
	}
}

// TestS3ArchiverKeyStaysUnderPrefixInRealStorage 验证清洗后的键在**真实存储**里
// 仍然落在 jobs/ 前缀之下。
//
// 键清洗已有单测（`TestObjectKeySanitizesTraversal`），但那条测的是字符串；
// 这里测的是它落到对象存储之后的事实 —— 含 `..` 的 jobID / 文件名
// 若能逃出前缀，按前缀授权的策略就被绕过了。
func TestS3ArchiverKeyStaysUnderPrefixInRealStorage(t *testing.T) {
	opt := requireS3(t)
	ctx := context.Background()

	arch, err := NewS3(opt, nil)
	if err != nil {
		t.Fatalf("构造 S3Archiver 失败: %v", err)
	}

	local := filepath.Join(t.TempDir(), "evil.mp4")
	if err := os.WriteFile(local, []byte("payload"), 0o644); err != nil {
		t.Fatalf("写本地文件失败: %v", err)
	}

	// 敌意输入：目录穿越 + 反斜杠 + 空格。
	key := ObjectKey("../../etc", KindFinal, `..\..\evil file.mp4`)
	if strings.Contains(key, "..") {
		t.Fatalf("对象键仍含 .. ：%q", key)
	}
	if _, err := arch.Put(ctx, local, key); err != nil {
		t.Fatalf("上传失败: %v", err)
	}

	cli := clientFor(t, opt)
	// 只按 jobs/ 前缀列举：能列到，说明它确实在受管前缀之下。
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()

	var listed []string
	// Recursive 必须为 true：minio-go 默认按 `/` 分隔返回**公共前缀**
	// （会得到 `jobs/etc/` 这种目录条目），拿不到对象键本身，
	// 断言"键有没有落在前缀下"就变成了断言空气。
	for obj := range cli.ListObjects(ctx, opt.Bucket, minio.ListObjectsOptions{
		Prefix:    "jobs/",
		Recursive: true,
	}) {
		if obj.Err != nil {
			t.Fatalf("列举对象失败: %v", obj.Err)
		}
		listed = append(listed, obj.Key)
	}
	found := false
	for _, k := range listed {
		if k == key {
			found = true
		}
		if strings.Contains(k, "..") {
			t.Errorf("存储里出现了含 .. 的键: %q", k)
		}
	}
	if !found {
		t.Errorf("对象键 %q 未出现在 jobs/ 前缀下，实际列举到: %v", key, listed)
	}
}

// TestS3ArchiverReportsMissingLocalFile 验证本地文件不存在时报错而不是伪造成功。
//
// 「看起来成功」的归档比失败危险得多：调用方据此认为产物已安全送出，
// 于是放行本地清理，最终两边都没有。
func TestS3ArchiverReportsMissingLocalFile(t *testing.T) {
	opt := requireS3(t)
	ctx := context.Background()

	arch, err := NewS3(opt, nil)
	if err != nil {
		t.Fatalf("构造 S3Archiver 失败: %v", err)
	}

	missing := filepath.Join(t.TempDir(), "does-not-exist.mp4")
	if _, err := arch.Put(ctx, missing, ObjectKey("job-e2e", KindFinal, "x.mp4")); err == nil {
		t.Fatal("本地文件不存在时 Put 应当报错")
	}
}

// TestS3ArchiverReportsUnreachableEndpoint 验证对象存储不可达时**如实报错**。
//
// 这条刻意用「端点连不上」而不是「凭据错误」来构造失败：真实的对象存储会拒绝
// 错误凭据，但**模拟器（moto 等）通常根本不校验凭据** —— 用凭据做断言，
// 测的就是服务端实现而非我们的代码，换个端点结论就翻。
// 连不上则是任何实现下都必须失败的情形，因此这条断言是可移植的。
//
// 为什么必须钉住它：归档报错若被吞掉，调用方会以为产物已安全送出，
// 于是放行本地清理 —— 最终两边都没有，而任务显示成功。
func TestS3ArchiverReportsUnreachableEndpoint(t *testing.T) {
	requireS3(t) // 与其他用例同一套门控：没有端点环境就不该跑这一组

	opt := S3Options{
		Endpoint:  "127.0.0.1:1", // 保留端口，必然无人监听
		AccessKey: "k",
		SecretKey: "s",
		Bucket:    "unreachable-bucket",
	}
	arch, err := NewS3(opt, nil)
	if err != nil {
		t.Fatalf("构造 S3Archiver 失败: %v", err)
	}

	local := filepath.Join(t.TempDir(), "f.mp4")
	if err := os.WriteFile(local, []byte("x"), 0o644); err != nil {
		t.Fatalf("写本地文件失败: %v", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	if _, err := arch.Put(ctx, local, ObjectKey("job-e2e", KindFinal, "f.mp4")); err == nil {
		t.Fatal("端点不可达时 Put 应当报错，而不是静默成功")
	}
}
