package assets

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func newTestStore(t *testing.T) *Store {
	t.Helper()
	s, err := NewStore(filepath.Join(t.TempDir(), "assets"))
	if err != nil {
		t.Fatalf("NewStore 失败: %v", err)
	}
	return s
}

// writeTemp 造一个"已经校验过"的临时文件，供 Save 收纳。
func writeTemp(t *testing.T, content string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "upload.tmp")
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatalf("写临时文件失败: %v", err)
	}
	return p
}

func TestSaveThenResolveRoundTrip(t *testing.T) {
	s := newTestStore(t)
	id, dst, err := s.Save("acme", writeTemp(t, "fake-audio"), ".mp3")
	if err != nil {
		t.Fatalf("Save 失败: %v", err)
	}
	if !assetIDRe.MatchString(id) {
		t.Errorf("id 形式不合法：%q", id)
	}
	if filepath.Ext(dst) != ".mp3" {
		t.Errorf("扩展名应当保留，实际 %q", dst)
	}

	got, err := s.Resolve("acme", id)
	if err != nil {
		t.Fatalf("Resolve 失败: %v", err)
	}
	if got != dst {
		t.Errorf("Resolve 应返回保存时的路径\n保存: %s\n解析: %s", dst, got)
	}

	// 跨租户不可见：素材是用户内容，隔离必须落到路径上。
	if _, err := s.Resolve("globex", id); err == nil {
		t.Error("别的租户不应能解析到这个素材")
	}
}

func TestResolveRejectsTraversalAndBadIDs(t *testing.T) {
	s := newTestStore(t)
	// 这些值如果被直接拿去拼路径，就等于把目录穿越做成了接口。
	bad := []string{
		"../../etc/passwd",
		"..\\..\\windows\\win.ini",
		"abc",                   // 太短，不像我们签发的 id
		strings.Repeat("a", 65), // 太长
		"ZZZZZZZZZZZZZZZZ",      // 非十六进制
		"../" + strings.Repeat("a", 16),
		"",
	}
	for _, id := range bad {
		if _, err := s.Resolve("acme", id); err == nil {
			t.Errorf("非法 asset_id %q 应当被拒绝", id)
		}
	}
}

func TestSaveRejectsUnknownExtension(t *testing.T) {
	s := newTestStore(t)
	for _, ext := range []string{".exe", ".sh", ".mp4", ""} {
		if _, _, err := s.Save("acme", writeTemp(t, "x"), ext); err == nil {
			t.Errorf("扩展名 %q 不该被接受", ext)
		}
	}
}

func TestSafeTenantNeutralisesPathCharacters(t *testing.T) {
	cases := map[string]string{
		"acme":         "acme",
		"a/b":          "a_b",
		"..":           "default", // 全是点：裁掉首尾的点后为空 -> default
		"../../etc":    "_.._etc", // 分隔符被替换，且首尾的点被裁掉
		"":             "default",
		"  ":           "__",
		"a b":          "a_b",
		"tenant-1_x.y": "tenant-1_x.y",
		"café":         "caf_",
		".hidden":      "hidden", // 不让租户名变成隐藏目录
	}
	for in, want := range cases {
		if got := SafeTenant(in); got != want {
			t.Errorf("SafeTenant(%q) = %q，期望 %q", in, got, want)
		}
	}
	// 两条关键性质：结果里不能有路径分隔符，也不能是 "." / ".."。
	for in := range cases {
		got := SafeTenant(in)
		if strings.ContainsAny(got, `/\`) {
			t.Errorf("SafeTenant(%q) 结果 %q 里仍有路径分隔符", in, got)
		}
		if got == "." || got == ".." {
			t.Errorf("SafeTenant(%q) 结果 %q 是目录穿越的特殊名", in, got)
		}
	}
}

func TestNewIDIsUniqueAndHex(t *testing.T) {
	seen := map[string]bool{}
	for i := 0; i < 64; i++ {
		id, err := NewID()
		if err != nil {
			t.Fatalf("NewID 失败: %v", err)
		}
		if !assetIDRe.MatchString(id) {
			t.Fatalf("id 形式不合法：%q", id)
		}
		if seen[id] {
			t.Fatalf("id 重复：%q（可预测的 id 等于让人枚举别人的素材）", id)
		}
		seen[id] = true
	}
}
