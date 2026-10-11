package assets

import (
	"archive/zip"
	"bytes"
	"encoding/json"
	"fmt"
	"image"
	"io"
	"math"
	"os"
	"path"
	"path/filepath"
	"strings"
)

var portraitStates = []string{"neutral", "explain", "think", "surprised"}

type portraitPose struct {
	Image     string `json:"image"`
	MouthOpen string `json:"mouth_open,omitempty"`
}
type portraitCue struct {
	Time  float64 `json:"time"`
	State string  `json:"state"`
}
type portraitManifest struct {
	Version int                     `json:"version"`
	States  map[string]portraitPose `json:"states"`
	Cues    []portraitCue           `json:"cues,omitempty"`
}

// ImportPortrait accepts a bounded data-only pack and canonicalizes all paths.
func (s *Store) ImportPortrait(tenant, archivePath, filename string) (receipt Live2DAsset, err error) {
	info, err := os.Stat(archivePath)
	if err != nil || info.Size() > MaxModelZipBytes {
		return receipt, fmt.Errorf("立绘 ZIP 不存在或超过 100 MB")
	}
	z, err := zip.OpenReader(archivePath)
	if err != nil {
		return receipt, fmt.Errorf("无法读取立绘 ZIP: %w", err)
	}
	defer z.Close()
	if len(z.File) > maxModelFiles {
		return receipt, fmt.Errorf("立绘文件数量超限")
	}
	files := map[string]*zip.File{}
	seen := map[string]bool{}
	manifestName := ""
	var expanded uint64
	for _, f := range z.File {
		name := strings.TrimSuffix(f.Name, "/")
		if !safeModelName(name) || f.Mode()&os.ModeSymlink != 0 {
			return receipt, fmt.Errorf("立绘 ZIP 含不安全路径")
		}
		if f.UncompressedSize64 > maxModelExpandedBytes-expanded {
			return receipt, fmt.Errorf("立绘解压大小超过 256 MB")
		}
		expanded += f.UncompressedSize64
		if f.FileInfo().IsDir() {
			continue
		}
		key := strings.ToLower(name)
		if seen[key] {
			return receipt, fmt.Errorf("立绘 ZIP 文件路径重复")
		}
		seen[key] = true
		if !strings.HasSuffix(key, ".png") && !strings.HasSuffix(key, ".json") {
			return receipt, fmt.Errorf("立绘包只接受 PNG 图片和 JSON 描述")
		}
		files[name] = f
		if path.Base(name) == "portrait.json" {
			if manifestName != "" {
				return receipt, fmt.Errorf("ZIP 必须只包含一个 portrait.json")
			}
			manifestName = name
		}
	}
	read := func(name string, limit int64) ([]byte, error) {
		f := files[name]
		if f == nil || f.UncompressedSize64 > uint64(limit) {
			return nil, fmt.Errorf("立绘文件不存在或过大：%s", name)
		}
		r, e := f.Open()
		if e != nil {
			return nil, e
		}
		defer r.Close()
		data, e := io.ReadAll(io.LimitReader(r, limit+1))
		if int64(len(data)) > limit {
			return nil, fmt.Errorf("立绘文件过大")
		}
		return data, e
	}
	if manifestName == "" {
		return receipt, fmt.Errorf("ZIP 缺少 portrait.json；请按立绘包说明打包")
	}
	data, err := read(manifestName, 1<<20)
	if err != nil {
		return receipt, err
	}
	var manifest portraitManifest
	if err = json.Unmarshal(data, &manifest); err != nil {
		return receipt, fmt.Errorf("立绘描述 JSON 无效")
	}
	if manifest.Version != 1 || len(manifest.States) != 4 || len(manifest.Cues) > 64 {
		return receipt, fmt.Errorf("立绘需 version=1、四种状态，时间表最多 64 项")
	}
	previous := -1.0
	for _, cue := range manifest.Cues {
		if _, ok := manifest.States[cue.State]; !ok || math.IsNaN(cue.Time) || math.IsInf(cue.Time, 0) || cue.Time < 0 || cue.Time > 1 || cue.Time <= previous {
			return receipt, fmt.Errorf("立绘时间表需 time=0～1 严格递增且引用已有状态")
		}
		previous = cue.Time
	}
	// Validate every pixel file before creating the final asset directory.
	images := map[string][]byte{}
	width, height, mouths := 0, 0, 0
	for _, state := range portraitStates {
		pose, ok := manifest.States[state]
		if !ok || pose.Image == "" {
			return receipt, fmt.Errorf("立绘缺少 %s 状态", state)
		}
		refs := []string{pose.Image}
		if pose.MouthOpen != "" {
			refs = append(refs, pose.MouthOpen)
			mouths++
		}
		for index, ref := range refs {
			if !safeModelName(ref) || !strings.HasSuffix(strings.ToLower(ref), ".png") {
				return receipt, fmt.Errorf("立绘只能引用包内 PNG 相对路径")
			}
			content, e := read(path.Join(path.Dir(manifestName), ref), 32<<20)
			if e != nil {
				return receipt, e
			}
			cfg, format, e := image.DecodeConfig(bytes.NewReader(content))
			if e != nil || format != "png" || cfg.Width < 1 || cfg.Height < 1 || cfg.Width > 4096 || cfg.Height > 4096 {
				return receipt, fmt.Errorf("立绘必须是有效 PNG，尺寸不超过 4096")
			}
			if width == 0 {
				width, height = cfg.Width, cfg.Height
			}
			if width != cfg.Width || height != cfg.Height {
				return receipt, fmt.Errorf("所有状态及口型图片须使用相同画布尺寸，避免切换跳动")
			}
			if _, _, e = image.Decode(bytes.NewReader(content)); e != nil {
				return receipt, fmt.Errorf("立绘 PNG 数据损坏")
			}
			name := state + ".png"
			if index == 1 {
				name = state + ".open.png"
				pose.MouthOpen = name
			} else {
				pose.Image = name
			}
			images[name] = content
		}
		manifest.States[state] = pose
	}
	if mouths != 0 && mouths != 4 {
		return receipt, fmt.Errorf("口型图须四种状态全部提供，或全部省略")
	}
	dir, err := s.live2DDir(tenant)
	if err != nil {
		return receipt, err
	}
	id, err := NewID()
	if err != nil {
		return receipt, err
	}
	dest := filepath.Join(dir, id+".portrait")
	if err = os.Mkdir(dest, 0o755); err != nil {
		return receipt, err
	}
	defer func() {
		if err != nil {
			_ = os.RemoveAll(dest)
		}
	}()
	for name, content := range images {
		if err = os.WriteFile(filepath.Join(dest, name), content, 0o644); err != nil {
			return receipt, err
		}
	}
	data, err = json.Marshal(manifest)
	if err != nil {
		return receipt, err
	}
	if err = os.WriteFile(filepath.Join(dest, "model.portrait.json"), data, 0o644); err != nil {
		return receipt, err
	}
	return Live2DAsset{AssetID: id, Filename: filepath.Base(filename), Kind: "portrait", States: portraitStates, LipSync: mouths == 4, MouthParameters: []string{}}, nil
}

// ResolvePresenter enforces tenant ownership for both supported presenter formats.
func (s *Store) ResolvePresenter(tenant, id string) (string, error) {
	if !assetIDRe.MatchString(id) {
		return "", fmt.Errorf("讲解员 asset_id 无效")
	}
	if file, err := s.ResolveLive2D(tenant, id); err == nil {
		return file, nil
	}
	dir, err := s.live2DDir(tenant)
	if err != nil {
		return "", err
	}
	file := filepath.Join(dir, id+".portrait", "model.portrait.json")
	info, err := os.Stat(file)
	if err != nil || !info.Mode().IsRegular() {
		return "", fmt.Errorf("讲解员素材不存在")
	}
	return file, nil
}
