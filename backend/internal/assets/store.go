// Package assets 管理用户上传的素材（目前只有背景音乐）。
//
// 设计上刻意**不引入任何新配置**：素材目录由已有的 `SCID_MEDIA_WORK_DIR`
// 派生（`<workDir>/assets`）。多一个配置项就多一处"部署时忘了配"的机会，
// 而这里并没有需要按环境变化的理由。
//
// 安全边界只有一条，但很硬：**请求方给出的任何字符串都不参与拼接磁盘路径**。
// 请求只能引用服务端签发过的 asset_id，路径一律由服务端自己拼。
// 这样"读服务端任意文件"就不会变成一个 HTTP 接口。
package assets

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

// assetIDRe 是 asset_id 的合法形式。
//
// 校验它是**路径安全的第一道闸门**：id 会直接参与拼路径，
// 放任 `../../etc/passwd` 之类的值进来就等于把目录穿越做成了接口。
var assetIDRe = regexp.MustCompile(`^[0-9a-f]{16,64}$`)

// allowedExt 是允许落盘的扩展名白名单。
//
// 注意它**只决定文件名怎么存**，不决定"这是不是一个音频文件" ——
// 后者必须由 ffprobe 真探一次（见 httpapi 的上传处理器）。
// 用扩展名判定内容是最经典的一类自欺：改个后缀就能绕过。
var allowedExt = map[string]bool{
	".mp3":  true,
	".wav":  true,
	".m4a":  true,
	".aac":  true,
	".flac": true,
	".ogg":  true,
	".opus": true,
}

// AllowedExt 报告扩展名是否在白名单内。
func AllowedExt(ext string) bool { return allowedExt[strings.ToLower(strings.TrimSpace(ext))] }

// AllowedExts 返回全部允许的扩展名（用于错误信息与文档，排序稳定）。
func AllowedExts() []string {
	out := make([]string, 0, len(allowedExt))
	for k := range allowedExt {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// Store 是素材目录的读写入口。
type Store struct {
	// root 是素材根目录（绝对路径）。
	root string
}

// NewStore 打开（必要时创建）素材目录。
func NewStore(root string) (*Store, error) {
	if strings.TrimSpace(root) == "" {
		return nil, fmt.Errorf("assets: 素材目录不能为空")
	}
	abs, err := filepath.Abs(root)
	if err != nil {
		return nil, fmt.Errorf("assets: 解析素材目录失败: %w", err)
	}
	if err := os.MkdirAll(abs, 0o755); err != nil {
		return nil, fmt.Errorf("assets: 创建素材目录失败 %s: %w", abs, err)
	}
	return &Store{root: abs}, nil
}

// TenantDir 返回某个租户的素材目录（并确保存在）。
//
// 按租户分目录：素材是用户内容，混在一个平铺目录里既难清理，
// 也让"这个文件属于谁"无法从路径上看出来。
func (s *Store) TenantDir(tenant string) (string, error) {
	dir := filepath.Join(s.root, SafeTenant(tenant))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", fmt.Errorf("assets: 创建租户素材目录失败: %w", err)
	}
	return dir, nil
}

// Save 把**已经校验过**的临时文件收进素材库，返回新 id 与最终路径。
//
// 为什么要先落临时文件再 Save：内容校验（ffprobe）必须在文件上做，
// 而校验不通过时不应该在素材库里留下半成品。收进来的那一步才生成正式 id。
func (s *Store) Save(tenant, srcPath, ext string) (id, dst string, err error) {
	ext = strings.ToLower(strings.TrimSpace(ext))
	if !allowedExt[ext] {
		return "", "", fmt.Errorf("assets: 不支持的素材扩展名 %q", ext)
	}
	dir, err := s.TenantDir(tenant)
	if err != nil {
		return "", "", err
	}
	id, err = NewID()
	if err != nil {
		return "", "", err
	}
	// 文件名完全由服务端生成：id 是随机十六进制、扩展名取自白名单，
	// 因此这里拼出来的路径**不可能**包含请求方给的任何片段。
	dst = filepath.Join(dir, id+ext)

	if err := os.Rename(srcPath, dst); err != nil {
		// 跨盘时 Rename 会失败（临时目录与素材库常常不在同一个盘），
		// 退回复制。这条路径在 Windows 上是常态而非例外。
		if cerr := copyFile(srcPath, dst); cerr != nil {
			return "", "", fmt.Errorf("assets: 收纳素材失败: %w（rename 也失败: %v）", cerr, err)
		}
	}
	return id, dst, nil
}

// Resolve 把 asset_id 解析成绝对路径。
func (s *Store) Resolve(tenant, id string) (string, error) {
	if !assetIDRe.MatchString(id) {
		return "", fmt.Errorf("assets: asset_id 形式非法 %q", id)
	}
	dir, err := s.TenantDir(tenant)
	if err != nil {
		return "", err
	}
	// 用 Glob 找 id.* ：扩展名由上传时决定，调用方不必知道。
	matches, err := filepath.Glob(filepath.Join(dir, id+".*"))
	if err != nil {
		return "", fmt.Errorf("assets: 解析素材失败: %w", err)
	}
	if len(matches) == 0 {
		return "", fmt.Errorf("assets: 素材不存在 %s", id)
	}
	return matches[0], nil
}

// NewID 生成一个新的素材 id（16 字节随机十六进制）。
//
// 用 crypto/rand 而不是自增序号：id 会出现在 API 响应里，
// 可预测的 id 等于让任何人枚举别人的素材。
func NewID() (string, error) {
	buf := make([]byte, 16)
	if _, err := rand.Read(buf); err != nil {
		return "", fmt.Errorf("assets: 生成素材 id 失败: %w", err)
	}
	return hex.EncodeToString(buf), nil
}

// SafeTenant 把租户名收敛成安全的目录名。
//
// 租户名来自 HTTP 头，是**不可信输入**，而它要参与拼路径 ——
// 所以这里只保留字母数字与 `-_.`，其余一律替换掉。
// 空名回落到 default，与租户中间件的缺省语义一致。
func SafeTenant(tenant string) string {
	var b strings.Builder
	for _, r := range tenant {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9',
			r == '-', r == '_', r == '.':
			b.WriteRune(r)
		default:
			b.WriteByte('_')
		}
	}
	out := strings.Trim(b.String(), ".")
	if out == "" {
		return "default"
	}
	if len(out) > 64 {
		out = out[:64]
	}
	return out
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.Create(dst)
	if err != nil {
		return err
	}
	defer out.Close()
	if _, err := io.Copy(out, in); err != nil {
		return err
	}
	return out.Sync()
}
