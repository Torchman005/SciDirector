package config

// 本文件覆盖**对象存储配置项的环境变量取名与迁移**。
//
// 背景（这是一处真实存在的缺陷，不是假想）：
// `.env.example` 与 compose 的公共环境里一直写着 `SCID_S3_*`，
// 而 Go 侧实际读取的是 `SCID_MINIO_*` —— **没有任何代码读 SCID_S3_**。
// 于是照着模板配 S3 的人会设一堆完全不起作用的变量，归档静默沿用默认值；
// 而「端点是空串」在归档路径上的表现与「压根没配置」无法区分，极难排查。
//
// 现在统一为 `SCID_S3_*`（后端已从 MinIO 换掉，名字里带 MINIO 本身就是误导），
// 并把 `SCID_MINIO_*` 保留为**兼容回退** —— 直接改名会让老部署的 .env 全部失效，
// 且同样是静默失效。下面这几条用例就是钉住这个迁移语义。

import (
	"testing"
)

// clearArchiveEnv 清掉所有可能干扰的变量，让断言不受运行环境影响。
//
// 测试环境里可能已经设置了其中任意一个（CI 或开发机的 .env），
// 不清干净会让用例的结果取决于环境，变成偶发失败。
func clearArchiveEnv(t *testing.T) {
	t.Helper()
	for _, k := range []string{
		"SCID_S3_ENDPOINT", "SCID_S3_ACCESS_KEY", "SCID_S3_SECRET_KEY",
		"SCID_S3_BUCKET", "SCID_S3_USE_SSL", "SCID_S3_REGION",
		"SCID_MINIO_ENDPOINT", "SCID_MINIO_ACCESS_KEY", "SCID_MINIO_SECRET_KEY",
		"SCID_MINIO_BUCKET", "SCID_MINIO_USE_SSL", "SCID_MINIO_REGION",
	} {
		t.Setenv(k, "")
	}
}

func TestArchiveEnvPrefersS3Names(t *testing.T) {
	clearArchiveEnv(t)
	t.Setenv("SCID_S3_ENDPOINT", "http://rustfs:9000")
	t.Setenv("SCID_S3_ACCESS_KEY", "new-key")
	t.Setenv("SCID_S3_SECRET_KEY", "new-secret")
	t.Setenv("SCID_S3_BUCKET", "new-bucket")
	t.Setenv("SCID_S3_USE_SSL", "true")
	// 同时也设上旧名：新名必须赢，否则迁移期内新旧并存时会连到错误的端点。
	t.Setenv("SCID_MINIO_ENDPOINT", "http://legacy-minio:9000")
	t.Setenv("SCID_MINIO_ACCESS_KEY", "old-key")
	t.Setenv("SCID_MINIO_BUCKET", "old-bucket")

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load 失败: %v", err)
	}

	if cfg.Archive.MinioEndpoint != "http://rustfs:9000" {
		t.Errorf("端点期望取 SCID_S3_ENDPOINT，实际 %q", cfg.Archive.MinioEndpoint)
	}
	if cfg.Archive.MinioAccessKey != "new-key" {
		t.Errorf("access key 期望取 SCID_S3_ACCESS_KEY，实际 %q", cfg.Archive.MinioAccessKey)
	}
	if cfg.Archive.MinioBucket != "new-bucket" {
		t.Errorf("bucket 期望取 SCID_S3_BUCKET，实际 %q", cfg.Archive.MinioBucket)
	}
	if !cfg.Archive.MinioUseSSL {
		t.Error("UseSSL 期望取 SCID_S3_USE_SSL=true")
	}
}

// TestArchiveEnvFallsBackToLegacyNames 是本文件最重要的一条：
// 老部署只设了 `SCID_MINIO_*` 时，配置**必须仍然读得到**。
//
// 这条一旦不成立，升级后那些机器的归档会静默失效 ——
// 要么报「endpoint 不能为空」，要么在没开归档的情况下什么都不发生。
func TestArchiveEnvFallsBackToLegacyNames(t *testing.T) {
	clearArchiveEnv(t)
	t.Setenv("SCID_MINIO_ENDPOINT", "http://legacy-minio:9000")
	t.Setenv("SCID_MINIO_ACCESS_KEY", "legacy-key")
	t.Setenv("SCID_MINIO_SECRET_KEY", "legacy-secret")
	t.Setenv("SCID_MINIO_BUCKET", "legacy-bucket")
	t.Setenv("SCID_MINIO_USE_SSL", "yes")

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load 失败: %v", err)
	}

	if cfg.Archive.MinioEndpoint != "http://legacy-minio:9000" {
		t.Errorf("旧名 SCID_MINIO_ENDPOINT 未被回退读取，实际 %q", cfg.Archive.MinioEndpoint)
	}
	if cfg.Archive.MinioAccessKey != "legacy-key" {
		t.Errorf("旧名 SCID_MINIO_ACCESS_KEY 未被回退读取，实际 %q", cfg.Archive.MinioAccessKey)
	}
	if cfg.Archive.MinioBucket != "legacy-bucket" {
		t.Errorf("旧名 SCID_MINIO_BUCKET 未被回退读取，实际 %q", cfg.Archive.MinioBucket)
	}
	if !cfg.Archive.MinioUseSSL {
		t.Error("旧名 SCID_MINIO_USE_SSL=yes 未被回退读取")
	}
}

// TestArchiveEnvDefaultsWhenUnset 钉住「什么都没配」时的取值。
//
// 缺省必须是**能跑通的那条路**：默认后端 none（不归档），
// 因此这里不能给端点编一个看似合理的默认值 ——
// 那会让「忘了配」看起来像「配好了」。
func TestArchiveEnvDefaultsWhenUnset(t *testing.T) {
	clearArchiveEnv(t)

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load 失败: %v", err)
	}

	if cfg.Archive.Backend != "none" {
		t.Errorf("缺省归档后端期望 none，实际 %q", cfg.Archive.Backend)
	}
	if cfg.Archive.MinioEndpoint != "" {
		t.Errorf("缺省端点期望空串（未配置必须看得出来），实际 %q", cfg.Archive.MinioEndpoint)
	}
	if cfg.Archive.MinioBucket != "scidirector" {
		t.Errorf("bucket 缺省期望 scidirector，实际 %q", cfg.Archive.MinioBucket)
	}
}
