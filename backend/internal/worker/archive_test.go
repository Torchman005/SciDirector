package worker

// 本文件覆盖 `finalizeArtifacts` —— 归档与本地清理的**交接处**。
//
// 为什么专门测这一段：它承载了一条很容易被顺手破坏、而且破坏后症状极隐蔽的不变式 ——
//
//	**归档失败时绝不清理本地。**
//
// 「宁可占盘，不可丢件」：磁盘可以加，数据丢了找不回来。一旦这条被破坏
// （比如有人在清理前忘了检查归档结果），表现不是报错，而是**产物凭空消失**：
// 任务显示成功、事件流一切正常，只有用户点开成片时才发现 404。
//
// 在写这个文件之前，`finalizeArtifacts` 只有调用点、没有任何测试 ——
// 归档后端的三条路径（none / local / s3）里，s3 更是连实例都没有过。
// 这里用**可控的归档器替身**把三条分支全部钉死，不依赖任何外部服务；
// 真实对象的往返则由 `archive/s3_e2e_test.go` 对着真 S3 端点验证。

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
	goredis "github.com/redis/go-redis/v9"

	"github.com/itJinYu/SciDirector/backend/internal/archive"
	"github.com/itJinYu/SciDirector/backend/internal/config"
	"github.com/itJinYu/SciDirector/backend/internal/store"
)

// recordingArchiver 是可控的归档器替身。
//
// 记录每次 Put 的 (本地路径, 对象键)，并可被指定为「总是失败」或「未启用」。
type recordingArchiver struct {
	kind     string
	enabled  bool
	failWith error
	puts     []putCall
}

type putCall struct {
	localPath string
	key       string
}

func (a *recordingArchiver) Put(_ context.Context, localPath, key string) (string, error) {
	a.puts = append(a.puts, putCall{localPath: localPath, key: key})
	if a.failWith != nil {
		return "", a.failWith
	}
	return "s3://test-bucket/" + key, nil
}

func (a *recordingArchiver) Enabled() bool { return a.enabled }
func (a *recordingArchiver) Kind() string  { return a.kind }

// archiveHarness 只装配 finalizeArtifacts 真正会用到的东西：
// Store（事件流需要）与归档器。ai / queue / media 在这条路径上不参与，
// 传 nil 反而能证明这一点 —— 一旦有人在这里引入隐藏依赖，测试会立刻 panic。
type archiveHarness struct {
	proc  *Processor
	store *store.Store
	work  string
	jobID string
}

func newArchiveHarness(t *testing.T, arch archive.Archiver, mutate func(*config.Config)) *archiveHarness {
	t.Helper()

	addr := requireRedis(t) // 复用 idempotency_test.go 里的探测与跳过逻辑
	ctx := context.Background()

	rdb := goredis.NewClient(&goredis.Options{Addr: addr, DB: testRedisDB})
	if err := rdb.FlushDB(ctx).Err(); err != nil {
		t.Fatalf("清空测试库失败: %v", err)
	}
	_ = rdb.Close()

	redisCfg := config.RedisConfig{Addr: addr, DB: testRedisDB}
	st, err := store.New(ctx, redisCfg)
	if err != nil {
		t.Fatalf("构造 Store 失败: %v", err)
	}
	t.Cleanup(func() { _ = st.Close() })

	work := t.TempDir()
	cfg := &config.Config{
		Env:      "test",
		LogLevel: "error",
		Redis:    redisCfg,
		Media:    config.MediaConfig{WorkDir: work},
		Archive:  config.ArchiveConfig{Backend: "s3"},
	}
	if mutate != nil {
		mutate(cfg)
	}

	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	return &archiveHarness{
		proc:  NewProcessor(cfg, st, nil, nil, nil, arch, logger),
		store: st,
		work:  work,
		jobID: "job-arch-" + strconv.FormatInt(time.Now().UnixNano(), 36),
	}
}

// seedWorkDir 造出一份贴近真实合成输出的本地目录：
// 成片、字幕、抽帧 PNG、归一化中间产物、单镜头片段各一。
//
// 命名刻意与 `archive.inferKind` 的推断规则对齐（`frames/` 目录 + `.png` 扩展名、
// 路径里含 `/normalized/`），否则测的就不是真实的分类行为了。
func (h *archiveHarness) seedWorkDir(t *testing.T) (finalPath, subtitlePath string) {
	t.Helper()
	write := func(rel, body string) string {
		p := filepath.Join(h.work, h.jobID, rel)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatalf("建目录失败: %v", err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatalf("写文件失败: %v", err)
		}
		return p
	}

	finalPath = write("final.mp4", "fake-final-mp4")
	subtitlePath = write("final.srt", "1\n00:00:00,000 --> 00:00:01,000\n你好\n")
	write(filepath.Join("frames", "frame_00.png"), "fake-png")
	write(filepath.Join("frames", "frame_01.png"), "fake-png")
	write(filepath.Join("normalized", "shot_000.mp4"), "fake-normalized")
	write(filepath.Join("shot_000", "out.mp4"), "fake-shot")
	return finalPath, subtitlePath
}

func exists(p string) bool {
	_, err := os.Stat(p)
	return err == nil
}

// TestFinalizeArtifactsCleansUpAfterSuccessfulArchive 覆盖成功路径。
//
// 归档成功之后，抽帧与归一化产物**必须**被删掉 —— 它们是本地卷被撑爆的
// 首要原因；而成片、字幕、单镜头片段必须留下（HITL 重做依赖后者）。
func TestFinalizeArtifactsCleansUpAfterSuccessfulArchive(t *testing.T) {
	arch := &recordingArchiver{kind: "s3", enabled: true}
	h := newArchiveHarness(t, arch, nil)
	finalPath, subtitlePath := h.seedWorkDir(t)
	ctx := context.Background()

	h.proc.finalizeArtifacts(ctx, h.jobID, finalPath, subtitlePath,
		filepath.Join(h.work, h.jobID), slog.New(slog.NewTextHandler(io.Discard, nil)))

	// 交付物与重做依赖的片段必须还在。
	for _, p := range []string{
		finalPath,
		subtitlePath,
		filepath.Join(h.work, h.jobID, "shot_000", "out.mp4"),
	} {
		if !exists(p) {
			t.Errorf("该保留的文件被删了: %s", p)
		}
	}
	// 抽帧与归一化产物必须被清掉。
	for _, p := range []string{
		filepath.Join(h.work, h.jobID, "frames", "frame_00.png"),
		filepath.Join(h.work, h.jobID, "normalized", "shot_000.mp4"),
	} {
		if exists(p) {
			t.Errorf("该清理的中间产物还在: %s", p)
		}
	}

	// 归档调用的对象键必须与 archive.ObjectKey 的约定一致。
	if len(arch.puts) != 2 {
		t.Fatalf("归档调用次数期望 2（成片 + 字幕），实际 %d: %+v", len(arch.puts), arch.puts)
	}
	wantFinalKey := archive.ObjectKey(h.jobID, archive.KindFinal, "final.mp4")
	if arch.puts[0].key != wantFinalKey {
		t.Errorf("成片对象键期望 %q，实际 %q", wantFinalKey, arch.puts[0].key)
	}
	wantSubKey := archive.ObjectKey(h.jobID, archive.KindSubtitle, "final.srt")
	if arch.puts[1].key != wantSubKey {
		t.Errorf("字幕对象键期望 %q，实际 %q", wantSubKey, arch.puts[1].key)
	}

	// 事件里必须留痕 backend 与 ok，否则前端与运维无从判断产物到底送出去没有。
	ev := lastEventFor(t, h, "compose")
	if ev.Payload["backend"] != "s3" {
		t.Errorf("事件 backend 期望 s3，实际 %v", ev.Payload["backend"])
	}
	if ev.Payload["ok"] != true {
		t.Errorf("归档成功时事件 ok 期望 true，实际 %v", ev.Payload["ok"])
	}
}

// TestFinalizeArtifactsKeepsLocalFilesWhenArchiveFails 是**最重要的那条**：
// 归档失败时一个本地文件都不能删。
//
// 这条不变式被破坏时的症状是「产物凭空消失」而不是报错 ——
// 任务成功、事件正常，用户点开成片才发现 404。
func TestFinalizeArtifactsKeepsLocalFilesWhenArchiveFails(t *testing.T) {
	arch := &recordingArchiver{kind: "s3", enabled: true, failWith: errors.New("对象存储不可达")}
	h := newArchiveHarness(t, arch, nil)
	finalPath, subtitlePath := h.seedWorkDir(t)
	ctx := context.Background()

	before := []string{
		finalPath,
		subtitlePath,
		filepath.Join(h.work, h.jobID, "frames", "frame_00.png"),
		filepath.Join(h.work, h.jobID, "frames", "frame_01.png"),
		filepath.Join(h.work, h.jobID, "normalized", "shot_000.mp4"),
		filepath.Join(h.work, h.jobID, "shot_000", "out.mp4"),
	}

	h.proc.finalizeArtifacts(ctx, h.jobID, finalPath, subtitlePath,
		filepath.Join(h.work, h.jobID), slog.New(slog.NewTextHandler(io.Discard, nil)))

	for _, p := range before {
		if !exists(p) {
			t.Errorf("归档失败却把本地文件删了（那可能就是唯一副本）: %s", p)
		}
	}

	ev := lastEventFor(t, h, "compose")
	if ev.Payload["ok"] != false {
		t.Errorf("归档失败时事件 ok 期望 false，实际 %v —— 前端会以为产物已经安全归档", ev.Payload["ok"])
	}
}

// TestFinalizeArtifactsNoopStillCleans 覆盖「没配对象存储」这条正常路径。
//
// 关键点：归档不发生时，本地清理**仍然要生效** —— 否则单机开发跑几次之后，
// 抽帧 PNG 就会把本地卷撑爆，而这条路正是缺省配置走的那条。
func TestFinalizeArtifactsNoopStillCleans(t *testing.T) {
	h := newArchiveHarness(t, archive.NoopArchiver{}, nil)
	finalPath, subtitlePath := h.seedWorkDir(t)
	ctx := context.Background()

	h.proc.finalizeArtifacts(ctx, h.jobID, finalPath, subtitlePath,
		filepath.Join(h.work, h.jobID), slog.New(slog.NewTextHandler(io.Discard, nil)))

	if exists(filepath.Join(h.work, h.jobID, "frames", "frame_00.png")) {
		t.Error("归档未启用时抽帧仍被清理 —— 缺省配置下本地卷会被撑爆")
	}
	if !exists(finalPath) {
		t.Error("成片必须在本地保留")
	}
	// 没启用归档就不该发归档事件（否则事件流里会出现一条没有后端的记录）。
	events, err := h.store.ListEvents(ctx, h.jobID, 0)
	if err != nil {
		t.Fatalf("读取事件失败: %v", err)
	}
	for _, ev := range events {
		if ev.Node == "compose" {
			t.Errorf("归档未启用却发出了归档事件: %+v", ev)
		}
	}
}

// lastEventFor 取出某个节点最后一条事件。
func lastEventFor(t *testing.T, h *archiveHarness, node string) *struct {
	Payload map[string]any
} {
	t.Helper()
	events, err := h.store.ListEvents(context.Background(), h.jobID, 0)
	if err != nil {
		t.Fatalf("读取事件失败: %v", err)
	}
	for i := len(events) - 1; i >= 0; i-- {
		if events[i].Node == node {
			return &struct{ Payload map[string]any }{Payload: events[i].Payload}
		}
	}
	t.Fatalf("没有找到 node=%q 的事件（共 %d 条）", node, len(events))
	return nil
}

// TestFinalizeArtifactsUploadsToRealS3 把两半合起来跑：**真实归档器 + 真实 finalizeArtifacts**。
//
// 上面两条各自成立并不等于接起来成立：archive 包的用例证明「S3Archiver 能传」，
// worker 的用例用替身证明「调用顺序与键正确」。但**传给归档器的本地路径是不是真的能读到**，
// 只有把真归档器插进去才知道 —— 工作目录、相对路径、清理时序任何一处错位，
// 都表现为「归档没上传成功但也没报错」，正是本项目反复踩的那类静默失败。
//
// 需要真实 S3 端点，未配置时跳过：
//
//	SCID_TEST_S3_ENDPOINT=http://127.0.0.1:9000 \
//	SCID_TEST_S3_ACCESS_KEY=... SCID_TEST_S3_SECRET_KEY=... \
//	go test ./internal/worker/ -run TestFinalizeArtifactsUploadsToRealS3 -v
func TestFinalizeArtifactsUploadsToRealS3(t *testing.T) {
	endpoint := strings.TrimSpace(os.Getenv("SCID_TEST_S3_ENDPOINT"))
	if endpoint == "" {
		t.Skip("未设置 SCID_TEST_S3_ENDPOINT，跳过「真实 S3 + finalizeArtifacts」联合验证")
	}
	accessKey, secretKey := os.Getenv("SCID_TEST_S3_ACCESS_KEY"), os.Getenv("SCID_TEST_S3_SECRET_KEY")
	if accessKey == "" || secretKey == "" {
		t.Skip("未设置 SCID_TEST_S3_ACCESS_KEY / SCID_TEST_S3_SECRET_KEY，跳过联合验证")
	}

	useSSL := strings.HasPrefix(endpoint, "https://")
	host := strings.TrimPrefix(strings.TrimPrefix(endpoint, "https://"), "http://")
	host = strings.TrimSuffix(host, "/")

	bucket := fmt.Sprintf("scid-joint-%d", time.Now().Unix()%1000000)
	arch, err := archive.NewS3(archive.S3Options{
		Endpoint:  host,
		AccessKey: accessKey,
		SecretKey: secretKey,
		Bucket:    bucket,
		UseSSL:    useSSL,
	}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatalf("构造真实 S3Archiver 失败: %v", err)
	}

	h := newArchiveHarness(t, arch, nil)
	finalPath, subtitlePath := h.seedWorkDir(t)

	finalBytes, err := os.ReadFile(finalPath)
	if err != nil {
		t.Fatalf("读取成片失败: %v", err)
	}

	h.proc.finalizeArtifacts(context.Background(), h.jobID, finalPath, subtitlePath,
		filepath.Join(h.work, h.jobID), slog.New(slog.NewTextHandler(io.Discard, nil)))

	// 用独立的客户端读回来比对 —— 不复用归档器自己的 client。
	cli, err := minio.New(host, &minio.Options{
		Creds:  credentials.NewStaticV4(accessKey, secretKey, ""),
		Secure: useSSL,
	})
	if err != nil {
		t.Fatalf("构造校验客户端失败: %v", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	key := archive.ObjectKey(h.jobID, archive.KindFinal, filepath.Base(finalPath))
	obj, err := cli.GetObject(ctx, bucket, key, minio.GetObjectOptions{})
	if err != nil {
		t.Fatalf("从对象存储取回成片失败（归档可能根本没上传成功）: %v", err)
	}
	defer func() { _ = obj.Close() }()

	got, err := io.ReadAll(obj)
	if err != nil {
		t.Fatalf("读取远端成片失败: %v", err)
	}
	if !bytes.Equal(got, finalBytes) {
		t.Errorf("远端成片与本地不一致：本地 %d 字节，远端 %d 字节", len(finalBytes), len(got))
	}

	// 归档成功后本地清理必须照常发生 —— 否则「验证过了」与「真的在跑」是两回事。
	if exists(filepath.Join(h.work, h.jobID, "frames", "frame_00.png")) {
		t.Error("归档成功后抽帧未被清理")
	}
	if !exists(finalPath) {
		t.Error("成片必须在本地保留")
	}
}
