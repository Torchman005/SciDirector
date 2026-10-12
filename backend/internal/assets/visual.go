package assets

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

const MaxVisualAssetsPerTenant = 48

// VisualAsset contains only user-authored descriptive metadata. File paths are
// always resolved from the tenant-scoped, server-issued asset ID.
type VisualAsset struct {
	AssetID     string `json:"asset_id"`
	Filename    string `json:"filename"`
	MIMEType    string `json:"mime_type"`
	Role        string `json:"role"`
	Description string `json:"description,omitempty"`
	Subject     string `json:"subject,omitempty"`
	Pose        string `json:"pose,omitempty"`
	Width       int    `json:"width"`
	Height      int    `json:"height"`
	SizeBytes   int64  `json:"size_bytes"`
}

// SaveVisual stores a validated PNG/JPEG and its manifest in a directory
// keyed by the complete tenant identity (sanitized names can alias).
func (s *Store) SaveVisual(tenant, srcPath, ext string, asset VisualAsset) (VisualAsset, string, error) {
	ext = strings.ToLower(strings.TrimSpace(ext))
	if ext != ".png" && ext != ".jpg" {
		return VisualAsset{}, "", fmt.Errorf("assets: visual extension %q is unsupported", ext)
	}
	if asset.MIMEType != "image/png" && asset.MIMEType != "image/jpeg" {
		return VisualAsset{}, "", fmt.Errorf("assets: visual MIME type is unsupported")
	}
	if (ext == ".png") != (asset.MIMEType == "image/png") {
		return VisualAsset{}, "", fmt.Errorf("assets: visual extension and MIME type do not match")
	}
	if asset.Width < 1 || asset.Height < 1 || asset.SizeBytes < 1 {
		return VisualAsset{}, "", fmt.Errorf("assets: visual dimensions and size must be positive")
	}
	dir, err := s.visualDir(tenant)
	if err != nil {
		return VisualAsset{}, "", err
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return VisualAsset{}, "", fmt.Errorf("assets: read visual library: %w", err)
	}
	count := 0
	for _, entry := range entries {
		if strings.HasSuffix(entry.Name(), ".meta.json") {
			count++
		}
	}
	if count >= MaxVisualAssetsPerTenant {
		return VisualAsset{}, "", fmt.Errorf("视觉素材库已满（每个账号最多 %d 张）", MaxVisualAssetsPerTenant)
	}
	id, err := NewID()
	if err != nil {
		return VisualAsset{}, "", err
	}
	asset.AssetID = id
	if asset.Filename == "" {
		asset.Filename = "visual" + ext
	}
	imagePath := filepath.Join(dir, id+ext)
	if err := copyFile(srcPath, imagePath); err != nil {
		return VisualAsset{}, "", fmt.Errorf("assets: save visual: %w", err)
	}
	manifestPath := filepath.Join(dir, id+".meta.json")
	data, err := json.Marshal(asset)
	if err != nil {
		_ = os.Remove(imagePath)
		return VisualAsset{}, "", fmt.Errorf("assets: encode visual metadata: %w", err)
	}
	tmp, err := os.CreateTemp(dir, id+"-*.tmp")
	if err == nil {
		_, err = tmp.Write(data)
		if closeErr := tmp.Close(); err == nil {
			err = closeErr
		}
	}
	if err == nil {
		err = os.Rename(tmp.Name(), manifestPath)
	}
	if err != nil {
		if tmp != nil {
			_ = os.Remove(tmp.Name())
		}
		_ = os.Remove(imagePath)
		return VisualAsset{}, "", fmt.Errorf("assets: save visual metadata: %w", err)
	}
	return asset, imagePath, nil
}

// ResolveVisual returns metadata and the canonical tenant-owned image path.
func (s *Store) ResolveVisual(tenant, id string) (VisualAsset, string, error) {
	if !assetIDRe.MatchString(id) {
		return VisualAsset{}, "", fmt.Errorf("assets: visual asset_id is invalid")
	}
	dir, err := s.visualDir(tenant)
	if err != nil {
		return VisualAsset{}, "", err
	}
	data, err := os.ReadFile(filepath.Join(dir, id+".meta.json"))
	if err != nil {
		return VisualAsset{}, "", fmt.Errorf("assets: visual asset does not exist")
	}
	var asset VisualAsset
	if err := json.Unmarshal(data, &asset); err != nil || asset.AssetID != id {
		return VisualAsset{}, "", fmt.Errorf("assets: visual metadata is invalid")
	}
	var ext string
	switch asset.MIMEType {
	case "image/png":
		ext = ".png"
	case "image/jpeg":
		ext = ".jpg"
	default:
		return VisualAsset{}, "", fmt.Errorf("assets: visual MIME type is invalid")
	}
	file := filepath.Join(dir, id+ext)
	info, err := os.Stat(file)
	if err != nil || !info.Mode().IsRegular() || info.Size() != asset.SizeBytes {
		return VisualAsset{}, "", fmt.Errorf("assets: visual image is missing or changed")
	}
	return asset, file, nil
}

// ListVisuals returns a stable, tenant-local list for the import library.
func (s *Store) ListVisuals(tenant string) ([]VisualAsset, error) {
	dir, err := s.visualDir(tenant)
	if err != nil {
		return nil, err
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, fmt.Errorf("assets: list visuals: %w", err)
	}
	out := make([]VisualAsset, 0, len(entries)/2)
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".meta.json") {
			continue
		}
		id := strings.TrimSuffix(entry.Name(), ".meta.json")
		asset, _, err := s.ResolveVisual(tenant, id)
		if err == nil {
			out = append(out, asset)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Filename < out[j].Filename })
	return out, nil
}

func (s *Store) visualDir(tenant string) (string, error) {
	identity := fmt.Sprintf("%x", sha256.Sum256([]byte(tenant)))
	dir := filepath.Join(s.root, "visuals", identity)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", fmt.Errorf("assets: create visual library: %w", err)
	}
	return dir, nil
}
