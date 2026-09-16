package archive

import (
	"context"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

// ---------------------------------------------------------------------------
// 对象键构造
// ---------------------------------------------------------------------------

func TestObjectKeyShape(t *testing.T) {
	got := ObjectKey("job-abc", KindFinal, "final.mp4")
	if got != "jobs/job-abc/final/final.mp4" {
		t.Fatalf("ObjectKey = %q", got)
	}
}

// TestObjectKeySanitizesTraversal 是这个函数存在的**主要理由**：
// filename 可能来自渲染器，含路径分隔符或 `..`。
// 拼进对象键会造成越界写入 —— 对象存储的键没有目录概念，
// 但按前缀授权的策略会因此被绕过。
func TestObjectKeySanitizesTraversal(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want string
	}{
		{"路径穿越", "../../etc/passwd", "passwd"},
		{"反斜杠穿越", `..\..\windows\system32\cmd.exe`, "cmd.exe"},
		{"绝对路径", "/etc/shadow", "shadow"},
		{"空文件名", "", "unnamed"},
		{"仅点号", "..", "unnamed"},
		{"空格与特殊字符", "my clip #1.mp4", "my_clip__1.mp4"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			key := ObjectKey("job-1", KindShot, c.in)
			if !strings.HasPrefix(key, "jobs/job-1/shot/") {
				t.Fatalf("键前缀不对: %q", key)
			}
			// 关键断言：清洗后的键里不允许出现穿越成分。
			rest := strings.TrimPrefix(key, "jobs/job-1/shot/")
			if strings.Contains(rest, "/") || strings.Contains(rest, "..") {
				t.Fatalf("文件名未被清洗干净，键为 %q", key)
			}
			if rest != c.want {
				t.Errorf("清洗结果 %q，期望 %q", rest, c.want)
			}
		})
	}
}

func TestObjectKeySanitizesJobID(t *testing.T) {
	key := ObjectKey("../evil", KindFinal, "a.mp4")
	if strings.Contains(key, "..") {
		t.Fatalf("jobID 未清洗: %q", key)
	}
}

// ---------------------------------------------------------------------------
// 保留策略（纯函数）
// ---------------------------------------------------------------------------

func entries() []Entry {
	return []Entry{
		{Path: "/w/final.mp4", Kind: KindFinal, SizeBytes: 10 << 20},
		{Path: "/w/final.srt", Kind: KindSubtitle, SizeBytes: 2 << 10},
		{Path: "/w/shots/s000.mp4", Kind: KindShot, SizeBytes: 5 << 20},
		{Path: "/w/normalized/norm_000.mp4", Kind: KindNormalized, SizeBytes: 8 << 20},
		{Path: "/w/frames/frame_00.png", Kind: KindFrame, SizeBytes: 1 << 20},
		{Path: "/w/frames/frame_01.png", Kind: KindFrame, SizeBytes: 1 << 20},
	}
}

func TestPlanCleanupKeepsDeliverables(t *testing.T) {
	plan := PlanCleanup(entries(), CleanupOptions{})

	// 成片、字幕、单镜头片段必须保留。
	for _, keep := range []string{"/w/final.mp4", "/w/final.srt", "/w/shots/s000.mp4"} {
		for _, p := range plan {
			if p == keep {
				t.Fatalf("交付物 %s 被列入删除计划 —— "+
					"成片是交付物、单镜头片段是 HITL 重做的输入，都不能删", keep)
			}
		}
	}
}

func TestPlanCleanupDeletesFramesAndNormalized(t *testing.T) {
	plan := PlanCleanup(entries(), CleanupOptions{})
	sort.Strings(plan)

	want := []string{
		"/w/frames/frame_00.png",
		"/w/frames/frame_01.png",
		"/w/normalized/norm_000.mp4",
	}
	if len(plan) != len(want) {
		t.Fatalf("删除计划 = %v，期望 %v", plan, want)
	}
	for i := range want {
		if plan[i] != want[i] {
			t.Errorf("plan[%d] = %q，期望 %q", i, plan[i], want[i])
		}
	}
}

// TestPlanCleanupFramesAreAlwaysDeleted 守住这条不变式：
// 抽帧 PNG 无论如何都要删（KeepNormalized 只影响归一化产物）。
//
// 它是本地卷被撑爆的首要原因：每个镜头好几张 PNG，
// 而且随时可以从视频重建，留着的唯一价值是排查。
func TestPlanCleanupFramesAreAlwaysDeleted(t *testing.T) {
	plan := PlanCleanup(entries(), CleanupOptions{KeepNormalized: true})
	hasFrame := false
	hasNormalized := false
	for _, p := range plan {
		if strings.Contains(p, "frames/") {
			hasFrame = true
		}
		if strings.Contains(p, "normalized/") {
			hasNormalized = true
		}
	}
	if !hasFrame {
		t.Error("抽帧 PNG 必须始终被删除")
	}
	if hasNormalized {
		t.Error("KeepNormalized=true 时不应删除归一化产物")
	}
}

func TestPlanCleanupKeepAllDeletesNothing(t *testing.T) {
	if plan := PlanCleanup(entries(), CleanupOptions{KeepAll: true}); len(plan) != 0 {
		t.Fatalf("KeepAll 时不应删除任何文件，实际 %v", plan)
	}
}

// TestPlanCleanupKeepsUnknownKinds 覆盖保守默认值。
// 删除不可逆，遇到没见过的类别时保留才是正确的默认。
func TestPlanCleanupKeepsUnknownKinds(t *testing.T) {
	plan := PlanCleanup([]Entry{
		{Path: "/w/mystery.bin", Kind: ObjectKind("who-knows")},
	}, CleanupOptions{})
	if len(plan) != 0 {
		t.Fatalf("未知类别不应被删除，实际 %v", plan)
	}
}

// ---------------------------------------------------------------------------
// 本地归档
// ---------------------------------------------------------------------------

func TestLocalArchiverRoundTrip(t *testing.T) {
	src := t.TempDir()
	dst := t.TempDir()

	file := filepath.Join(src, "final.mp4")
	content := []byte("not really a video, but the bytes must survive")
	if err := os.WriteFile(file, content, 0o644); err != nil {
		t.Fatalf("准备源文件失败: %v", err)
	}

	a, err := NewLocal(dst)
	if err != nil {
		t.Fatalf("构造本地归档器失败: %v", err)
	}
	if !a.Enabled() || a.Kind() != "local" {
		t.Fatalf("Enabled/Kind 异常: %v/%s", a.Enabled(), a.Kind())
	}

	uri, err := a.Put(context.Background(), file, ObjectKey("job-1", KindFinal, "final.mp4"))
	if err != nil {
		t.Fatalf("归档失败: %v", err)
	}
	if !strings.HasPrefix(uri, "file://") {
		t.Fatalf("URI 前缀异常: %q", uri)
	}

	got, err := os.ReadFile(filepath.Join(dst, "jobs", "job-1", "final", "final.mp4"))
	if err != nil {
		t.Fatalf("读取归档产物失败: %v", err)
	}
	if string(got) != string(content) {
		t.Fatalf("归档内容不一致：%q", string(got))
	}
}

func TestLocalArchiverRejectsMissingSource(t *testing.T) {
	a, err := NewLocal(t.TempDir())
	if err != nil {
		t.Fatalf("构造失败: %v", err)
	}
	if _, err := a.Put(context.Background(), "/nonexistent/x.mp4", "k"); err == nil {
		t.Fatal("源文件不存在时应报错")
	}
}

// TestLocalArchiverRejectsDirectory 防止把目录当成文件复制。
// 静默"成功"地归档一个目录，会让下游拿到一个空对象。
func TestLocalArchiverRejectsDirectory(t *testing.T) {
	dir := t.TempDir()
	sub := filepath.Join(dir, "adir")
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatalf("建目录失败: %v", err)
	}
	a, _ := NewLocal(t.TempDir())
	if _, err := a.Put(context.Background(), sub, "k"); err == nil {
		t.Fatal("源路径是目录时应报错")
	}
}

func TestNewLocalRejectsEmptyRoot(t *testing.T) {
	if _, err := NewLocal("  "); err == nil {
		t.Fatal("空归档目录应报错，而不是静默退化成一个什么都不归档的实现")
	}
}

// ---------------------------------------------------------------------------
// 工厂
// ---------------------------------------------------------------------------

func TestFactoryBackendSelection(t *testing.T) {
	t.Run("空配置退化为 noop", func(t *testing.T) {
		a, err := New(Options{}, nil)
		if err != nil {
			t.Fatalf("不应报错: %v", err)
		}
		if a.Enabled() {
			t.Fatal("未配置时不应启用归档")
		}
		if _, ok := a.(NoopArchiver); !ok {
			t.Fatalf("期望 NoopArchiver，实际 %T", a)
		}
	})

	t.Run("local", func(t *testing.T) {
		a, err := New(Options{Backend: "local", LocalDir: t.TempDir()}, nil)
		if err != nil {
			t.Fatalf("不应报错: %v", err)
		}
		if a.Kind() != "local" {
			t.Fatalf("期望 local，实际 %s", a.Kind())
		}
	})

	// 拼错后端名必须报错：静默退化为 none 会在磁盘写满那天才被发现。
	t.Run("未知后端报错", func(t *testing.T) {
		if _, err := New(Options{Backend: "s4"}, nil); err == nil {
			t.Fatal("未知后端应报错")
		}
	})

	// s3 配置不完整必须报错，而不是悄悄回退到 local：
	// 静默改变产物去向比直接失败危险得多。
	t.Run("s3 缺 endpoint 报错", func(t *testing.T) {
		if _, err := New(Options{Backend: "s3"}, nil); err == nil {
			t.Fatal("s3 缺 endpoint 应报错")
		}
	})
}

func TestS3ArchiverConstruction(t *testing.T) {
	a, err := NewS3(S3Options{
		Endpoint: "127.0.0.1:9000", AccessKey: "minioadmin",
		SecretKey: "minioadmin", Bucket: "scidirector",
	}, nil)
	if err != nil {
		t.Fatalf("构造 S3 归档器失败: %v", err)
	}
	if !a.Enabled() || a.Kind() != "s3" {
		t.Fatalf("Enabled/Kind 异常: %v/%s", a.Enabled(), a.Kind())
	}

	if _, err := NewS3(S3Options{AccessKey: "x"}, nil); err == nil {
		t.Fatal("缺 endpoint 应报错")
	}
	if _, err := NewS3(S3Options{Endpoint: "127.0.0.1:9000"}, nil); err == nil {
		t.Fatal("缺 bucket 应报错")
	}
}

// TestContentTypeForIsPreviewFriendly 守住内容类型：
// 给错类型时浏览器会下载而不是内联预览，审核台点开成片却开始下载。
func TestContentTypeForIsPreviewFriendly(t *testing.T) {
	cases := map[string]string{
		"jobs/j/final/final.mp4": "video/mp4",
		"jobs/j/final/final.srt": "application/x-subrip",
		"jobs/j/shot/a.png":      "image/png",
		"jobs/j/final/data.json": "application/json",
		"jobs/j/final/weird.xyz": "application/octet-stream",
	}
	for key, want := range cases {
		if got := contentTypeFor(key); got != want {
			t.Errorf("contentTypeFor(%q) = %q，期望 %q", key, got, want)
		}
	}
}

// ---------------------------------------------------------------------------
// 目录扫描
// ---------------------------------------------------------------------------

func TestCollectEntriesInfersKinds(t *testing.T) {
	root := t.TempDir()
	mustWrite := func(rel string, data string) {
		p := filepath.Join(root, rel)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatalf("建目录失败: %v", err)
		}
		if err := os.WriteFile(p, []byte(data), 0o644); err != nil {
			t.Fatalf("写文件失败: %v", err)
		}
	}
	mustWrite("final.mp4", "x")
	mustWrite("final.srt", "x")
	mustWrite("normalized/norm_000.mp4", "x")
	mustWrite("frames/frame_00000.png", "x")
	mustWrite("shots/s000.mp4", "x")

	entries, err := CollectEntries(root)
	if err != nil {
		t.Fatalf("扫描失败: %v", err)
	}
	byName := map[string]ObjectKind{}
	for _, e := range entries {
		rel, _ := filepath.Rel(root, e.Path)
		byName[filepath.ToSlash(rel)] = e.Kind
	}

	want := map[string]ObjectKind{
		"final.mp4":               KindShot,
		"final.srt":               KindSubtitle,
		"normalized/norm_000.mp4": KindNormalized,
		"frames/frame_00000.png":  KindFrame,
		"shots/s000.mp4":          KindShot,
	}
	for name, kind := range want {
		if byName[name] != kind {
			t.Errorf("%s 推断为 %q，期望 %q", name, byName[name], kind)
		}
	}
	// 端到端串起来：扫描 → 计划 → 只有中间产物被列入删除。
	plan := PlanCleanup(entries, CleanupOptions{})
	for _, p := range plan {
		rel, _ := filepath.Rel(root, p)
		switch filepath.ToSlash(rel) {
		case "normalized/norm_000.mp4", "frames/frame_00000.png":
		default:
			t.Errorf("不应删除 %s", rel)
		}
	}
}

// TestCleanupIsBestEffort 覆盖"尽力而为"语义：
// 单个文件删不掉不应让整个清理失败（清理失败不该把成功的任务判成失败）。
func TestCleanupIsBestEffort(t *testing.T) {
	dir := t.TempDir()
	good := filepath.Join(dir, "a.png")
	if err := os.WriteFile(good, []byte("x"), 0o644); err != nil {
		t.Fatalf("准备文件失败: %v", err)
	}

	removed, failed := Cleanup([]string{good, filepath.Join(dir, "does-not-exist")})
	// os.RemoveAll 对不存在的路径返回 nil，因此两个都会"成功"。
	if removed != 2 {
		t.Fatalf("removed = %d，期望 2", removed)
	}
	if len(failed) != 0 {
		t.Fatalf("不应有失败项: %v", failed)
	}
	if _, err := os.Stat(good); !os.IsNotExist(err) {
		t.Fatal("文件应已被删除")
	}
}
