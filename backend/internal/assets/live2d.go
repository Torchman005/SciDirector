package assets

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"image"
	_ "image/jpeg"
	_ "image/png"
	"io"
	"os"
	"path"
	"path/filepath"
	"strings"
)

// Model uploads contain data only. Executable SDK code is deployment-owned.
const MaxModelZipBytes int64 = 100 << 20
const maxModelExpandedBytes uint64 = 256 << 20
const maxModelFiles = 256

// Live2DAsset is the public import receipt; filesystem paths remain server-side.
type Live2DAsset struct {
	AssetID         string   `json:"asset_id"`
	Filename        string   `json:"filename"`
	MouthParameters []string `json:"mouth_parameters"`
}

type modelManifest struct {
	Version        int `json:"Version"`
	FileReferences struct {
		Moc      string   `json:"Moc"`
		Textures []string `json:"Textures"`
	} `json:"FileReferences"`
	Groups []struct {
		Target string   `json:"Target"`
		Name   string   `json:"Name"`
		IDs    []string `json:"Ids"`
	} `json:"Groups,omitempty"`
	Layout map[string]float64 `json:"Layout,omitempty"`
}

func safeModelName(name string) bool {
	if name == "" || strings.ContainsAny(name, "\\:\x00?#%") || strings.HasPrefix(name, "/") || path.Clean(name) != name {
		return false
	}
	for _, part := range strings.Split(name, "/") {
		if part == "." || part == ".." || strings.TrimSpace(part) != part || strings.HasSuffix(part, ".") {
			return false
		}
		// Reject Windows device aliases on every host so imported packages are portable.
		base := strings.ToUpper(strings.SplitN(part, ".", 2)[0])
		if base == "CON" || base == "PRN" || base == "AUX" || base == "NUL" ||
			(len(base) == 4 && (strings.HasPrefix(base, "COM") || strings.HasPrefix(base, "LPT")) && base[3] >= '1' && base[3] <= '9') {
			return false
		}
	}
	return true
}

// ImportLive2D validates the whole archive, then extracts only the model and textures.
// Motion/audio/script files are never executed; idle motion is deterministic runtime code.
func (s *Store) ImportLive2D(tenant, archivePath, filename string) (receipt Live2DAsset, err error) {
	info, err := os.Stat(archivePath)
	if err != nil || info.Size() > MaxModelZipBytes {
		return receipt, fmt.Errorf("Live2D ZIP 不存在或超过 100 MB")
	}
	z, err := zip.OpenReader(archivePath)
	if err != nil {
		return receipt, fmt.Errorf("Live2D ZIP 无法读取: %w", err)
	}
	defer z.Close()
	if len(z.File) > maxModelFiles {
		return receipt, fmt.Errorf("Live2D 文件数量超过 %d", maxModelFiles)
	}
	files := map[string]*zip.File{}
	seen := map[string]bool{}
	modelName := ""
	var expanded uint64
	for _, f := range z.File {
		name := strings.TrimSuffix(f.Name, "/")
		if !safeModelName(name) || f.Mode()&os.ModeSymlink != 0 {
			return receipt, fmt.Errorf("Live2D ZIP 含不安全路径")
		}
		key := strings.ToLower(name)
		if seen[key] {
			return receipt, fmt.Errorf("Live2D ZIP 含重复路径")
		}
		seen[key] = true
		if f.UncompressedSize64 > maxModelExpandedBytes-expanded {
			return receipt, fmt.Errorf("Live2D 解压总大小超过 256 MB")
		}
		expanded += f.UncompressedSize64
		if f.FileInfo().IsDir() {
			continue
		}
		if strings.HasSuffix(key, ".js") || strings.HasSuffix(key, ".html") || strings.HasSuffix(key, ".exe") {
			return receipt, fmt.Errorf("模型 ZIP 不接受可执行脚本；请只打包模型数据")
		}
		files[name] = f
		if strings.HasSuffix(key, ".model3.json") {
			if modelName != "" {
				return receipt, fmt.Errorf("ZIP 必须只包含一个 .model3.json")
			}
			modelName = name
		}
	}
	if modelName == "" {
		return receipt, fmt.Errorf("ZIP 缺少 Cubism 3/4 的 .model3.json")
	}
	read := func(name string, limit int64) ([]byte, error) {
		f := files[name]
		if f == nil {
			return nil, fmt.Errorf("模型引用的文件不存在：%s", name)
		}
		if f.UncompressedSize64 > uint64(limit) {
			return nil, fmt.Errorf("模型文件过大：%s", name)
		}
		r, e := f.Open()
		if e != nil {
			return nil, e
		}
		defer r.Close()
		b, e := io.ReadAll(io.LimitReader(r, limit+1))
		if int64(len(b)) > limit {
			return nil, fmt.Errorf("模型文件过大")
		}
		return b, e
	}
	manifestData, err := read(modelName, 1<<20)
	if err != nil {
		return receipt, err
	}
	var manifest modelManifest
	if err = json.Unmarshal(manifestData, &manifest); err != nil {
		return receipt, fmt.Errorf("模型描述 JSON 无效: %w", err)
	}
	if manifest.Version != 3 || !strings.HasSuffix(strings.ToLower(manifest.FileReferences.Moc), ".moc3") || len(manifest.FileReferences.Textures) == 0 || len(manifest.FileReferences.Textures) > 16 {
		return receipt, fmt.Errorf("模型必须含 Version=3、.moc3 和 1～16 张纹理")
	}
	dir, err := s.live2DDir(tenant)
	if err != nil {
		return receipt, err
	}
	id, err := NewID()
	if err != nil {
		return receipt, err
	}
	dest := filepath.Join(dir, id+".live2d")
	if err = os.Mkdir(dest, 0o755); err != nil {
		return receipt, err
	}
	defer func() {
		if err != nil {
			_ = os.RemoveAll(dest)
		}
	}()
	refs := append([]string{manifest.FileReferences.Moc}, manifest.FileReferences.Textures...)
	for i, ref := range refs {
		if !safeModelName(ref) {
			return receipt, fmt.Errorf("模型只能引用 ZIP 内的相对文件")
		}
		data, e := read(path.Join(path.Dir(modelName), ref), 64<<20)
		if e != nil {
			return receipt, e
		}
		if i == 0 {
			if len(data) < 4 || string(data[:4]) != "MOC3" {
				return receipt, fmt.Errorf("moc3 文件头无效")
			}
		} else {
			cfg, format, e := image.DecodeConfig(bytes.NewReader(data))
			if e != nil || (format != "png" && format != "jpeg") || cfg.Width < 1 || cfg.Height < 1 || cfg.Width > 8192 || cfg.Height > 8192 {
				return receipt, fmt.Errorf("模型纹理不是有效 PNG/JPEG 或尺寸超过 8192")
			}
		}
		file := filepath.Join(dest, filepath.FromSlash(ref))
		if e = os.MkdirAll(filepath.Dir(file), 0o755); e != nil {
			return receipt, e
		}
		if e = os.WriteFile(file, data, 0o644); e != nil {
			return receipt, e
		}
	}
	// Marshal the allowlisted schema: imported JavaScript/URLs/motions never reach the renderer.
	data, err := json.Marshal(manifest)
	if err != nil {
		return receipt, err
	}
	if err = os.WriteFile(filepath.Join(dest, "model.model3.json"), data, 0o644); err != nil {
		return receipt, err
	}
	receipt = Live2DAsset{AssetID: id, Filename: filepath.Base(filename), MouthParameters: []string{}}
	for _, group := range manifest.Groups {
		if group.Name == "LipSync" && group.Target == "Parameter" {
			receipt.MouthParameters = append(receipt.MouthParameters, group.IDs...)
		}
	}
	return receipt, nil
}

// ResolveLive2D accepts only a tenant-owned import receipt.
func (s *Store) ResolveLive2D(tenant, id string) (string, error) {
	if !assetIDRe.MatchString(id) {
		return "", fmt.Errorf("Live2D asset_id 无效")
	}
	dir, err := s.live2DDir(tenant)
	if err != nil {
		return "", err
	}
	file := filepath.Join(dir, id+".live2d", "model.model3.json")
	info, err := os.Stat(file)
	if err != nil || !info.Mode().IsRegular() {
		return "", fmt.Errorf("Live2D 素材不存在")
	}
	return file, nil
}

func (s *Store) live2DDir(tenant string) (string, error) {
	// Sanitizing names alone aliases tenants like a/b and a_b. New model storage
	// uses the full tenant identity without migrating existing audio assets.
	dir := filepath.Join(s.root, "live2d", fmt.Sprintf("%x", sha256.Sum256([]byte(tenant))))
	return dir, os.MkdirAll(dir, 0o755)
}
