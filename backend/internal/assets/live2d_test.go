package assets

import (
	"archive/zip"
	"bytes"
	"image"
	"image/png"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func modelZIP(t *testing.T, extra map[string][]byte, repeatedEntries ...string) string {
	t.Helper()
	var texture bytes.Buffer
	_ = png.Encode(&texture, image.NewRGBA(image.Rect(0, 0, 4, 4)))
	files := map[string][]byte{"model/a.model3.json": []byte(`{"Version":3,"FileReferences":{"Moc":"a.moc3","Textures":["textures/a.png"],"Motions":{"Idle":[{"File":"evil.js"}]}},"Groups":[{"Name":"LipSync","Target":"Parameter","Ids":["ParamMouthOpenY"]}]}`), "model/a.moc3": []byte("MOC3test"), "model/textures/a.png": texture.Bytes()}
	for k, v := range extra {
		files[k] = v
	}
	file := filepath.Join(t.TempDir(), "model.zip")
	f, e := os.Create(file)
	if e != nil {
		t.Fatal(e)
	}
	z := zip.NewWriter(f)
	for k, v := range files {
		w, e := z.Create(k)
		if e != nil {
			t.Fatal(e)
		}
		_, _ = w.Write(v)
	}
	for _, name := range repeatedEntries {
		w, e := z.Create(name)
		if e != nil {
			t.Fatal(e)
		}
		_, _ = w.Write(files[name])
	}
	if e = z.Close(); e != nil {
		t.Fatal(e)
	}
	_ = f.Close()
	return file
}

func TestLive2DRepeatedDirectoryRecordsAreHarmless(t *testing.T) {
	s, e := NewStore(t.TempDir())
	if e != nil {
		t.Fatal(e)
	}
	if _, e = s.ImportLive2D("tenant", modelZIP(t, nil, "model/", "model/"), "model.zip"); e != nil {
		t.Fatalf("repeated directory entries should import: %v", e)
	}
}

func TestLive2DDuplicateFileReportsPath(t *testing.T) {
	s, e := NewStore(t.TempDir())
	if e != nil {
		t.Fatal(e)
	}
	_, e = s.ImportLive2D("tenant", modelZIP(t, nil, "model/a.moc3"), "model.zip")
	if e == nil || !strings.Contains(e.Error(), "model/a.moc3") {
		t.Fatalf("duplicate file path should be named: %v", e)
	}
}

func TestLive2DImportIsDataOnlyAndTenantIsolated(t *testing.T) {
	s, e := NewStore(t.TempDir())
	if e != nil {
		t.Fatal(e)
	}
	a, e := s.ImportLive2D("a/b", modelZIP(t, nil), "模型.zip")
	if e != nil {
		t.Fatal(e)
	}
	model, e := s.ResolveLive2D("a/b", a.AssetID)
	if e != nil {
		t.Fatal(e)
	}
	if _, e = s.ResolveLive2D("a_b", a.AssetID); e == nil {
		t.Fatal("tenant alias escaped")
	}
	data, e := os.ReadFile(model)
	if e != nil {
		t.Fatal(e)
	}
	if bytes.Contains(data, []byte("evil")) || bytes.Contains(data, []byte("Motions")) {
		t.Fatal("unsafe optional references preserved")
	}
	if len(a.MouthParameters) != 1 || a.Filename != "模型.zip" {
		t.Fatalf("bad receipt: %+v", a)
	}
}

func TestLive2DRejectsTraversalScriptsDuplicatesAndBrokenModels(t *testing.T) {
	cases := []map[string][]byte{
		{"model/CON.png": []byte("bad")}, {"model/LPT1": []byte("bad")},
		{"../escape": []byte("bad")}, {"model/evil.js": []byte("alert(1)")},
		{"model/a.moc3": []byte("bad")}, {"model/textures/a.png": []byte("not image")},
		{"model/A.moc3": []byte("MOC3dup")}, {"model/second.model3.json": []byte("{}")},
		{"model/a.model3.json": []byte(`{"Version":3,"FileReferences":{"Moc":"../../secret.moc3","Textures":["textures/a.png"]}}`)},
	}
	for _, extra := range cases {
		s, _ := NewStore(t.TempDir())
		if _, e := s.ImportLive2D("tenant", modelZIP(t, extra), "x.zip"); e == nil {
			t.Fatalf("accepted malicious input: %v", extra)
		}
	}
}
