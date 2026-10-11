package assets

import (
	"archive/zip"
	"bytes"
	"encoding/json"
	"image"
	"image/png"
	"os"
	"path/filepath"
	"testing"
)

func portraitZIP(t *testing.T, change func(*portraitManifest, map[string][]byte)) string {
	t.Helper()
	var picture bytes.Buffer
	_ = png.Encode(&picture, image.NewNRGBA(image.Rect(0, 0, 8, 12)))
	files := map[string][]byte{"a.png": picture.Bytes()}
	manifest := portraitManifest{Version: 1, States: map[string]portraitPose{}}
	for _, state := range portraitStates {
		manifest.States[state] = portraitPose{Image: "a.png", MouthOpen: "a.png"}
	}
	manifest.Cues = []portraitCue{{Time: .4, State: "think"}, {Time: .75, State: "surprised"}}
	if change != nil {
		change(&manifest, files)
	}
	data, _ := json.Marshal(manifest)
	files["portrait.json"] = data
	file := filepath.Join(t.TempDir(), "poses.zip")
	out, _ := os.Create(file)
	writer := zip.NewWriter(out)
	for name, content := range files {
		w, e := writer.Create(name)
		if e != nil {
			t.Fatal(e)
		}
		_, _ = w.Write(content)
	}
	if e := writer.Close(); e != nil {
		t.Fatal(e)
	}
	_ = out.Close()
	return file
}

func TestPortraitImportAndTenantBoundary(t *testing.T) {
	s, _ := NewStore(t.TempDir())
	receipt, err := s.ImportPortrait("a", portraitZIP(t, nil), "poses.zip")
	if err != nil {
		t.Fatal(err)
	}
	if receipt.Kind != "portrait" || !receipt.LipSync || len(receipt.States) != 4 {
		t.Fatalf("bad receipt %+v", receipt)
	}
	file, err := s.ResolvePresenter("a", receipt.AssetID)
	if err != nil {
		t.Fatal(err)
	}
	var manifest portraitManifest
	data, _ := os.ReadFile(file)
	_ = json.Unmarshal(data, &manifest)
	if manifest.States["think"].Image != "think.png" {
		t.Fatal("image paths not canonicalized")
	}
	if _, err = s.ResolvePresenter("b", receipt.AssetID); err == nil {
		t.Fatal("cross-tenant access")
	}
}

func TestPortraitRejectsBadPacks(t *testing.T) {
	cases := map[string]func(*portraitManifest, map[string][]byte){
		"traversal":      func(m *portraitManifest, f map[string][]byte) { f["../evil.png"] = f["a.png"] },
		"script":         func(m *portraitManifest, f map[string][]byte) { f["evil.js"] = []byte("alert(1)") },
		"missing pose":   func(m *portraitManifest, f map[string][]byte) { delete(m.States, "think") },
		"partial mouths": func(m *portraitManifest, f map[string][]byte) { m.States["think"] = portraitPose{Image: "a.png"} },
		"outside ref":    func(m *portraitManifest, f map[string][]byte) { m.States["think"] = portraitPose{Image: "../a.png"} },
		"invalid cue":    func(m *portraitManifest, f map[string][]byte) { m.Cues = []portraitCue{{Time: .3, State: "unknown"}} },
		"reversed cue": func(m *portraitManifest, f map[string][]byte) {
			m.Cues = []portraitCue{{Time: .7, State: "think"}, {Time: .2, State: "neutral"}}
		},
		"corrupt png": func(m *portraitManifest, f map[string][]byte) { f["a.png"] = []byte("not an image") },
	}
	for name, change := range cases {
		t.Run(name, func(t *testing.T) {
			s, _ := NewStore(t.TempDir())
			if _, err := s.ImportPortrait("a", portraitZIP(t, change), "poses.zip"); err == nil {
				t.Fatal("invalid pack accepted")
			}
		})
	}
}

func TestPortraitWithoutMouthsDoesNotClaimLipSync(t *testing.T) {
	s, _ := NewStore(t.TempDir())
	receipt, err := s.ImportPortrait("a", portraitZIP(t, func(m *portraitManifest, _ map[string][]byte) {
		for name, pose := range m.States {
			pose.MouthOpen = ""
			m.States[name] = pose
		}
	}), "poses.zip")
	if err != nil || receipt.LipSync {
		t.Fatalf("%+v %v", receipt, err)
	}
}
