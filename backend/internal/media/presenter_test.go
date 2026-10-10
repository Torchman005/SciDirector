package media

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

func TestPresenterGeometryPreservesDiagramAndSubtitleLane(t *testing.T) {
	for _, size := range [][2]int{{1920, 1080}, {1080, 1920}, {1280, 720}, {321, 181}} {
		cw, ch, aw, ah := PresenterGeometry(size[0], size[1])
		if cw+aw > size[0] || ch > size[1] || ah > size[1] || cw%2 != 0 || ch%2 != 0 || aw%2 != 0 || ah%2 != 0 {
			t.Fatalf("bad geometry %v", size)
		}
	}
}

func TestOverlayPresenterPreservesDurationAudioAndAlpha(t *testing.T) {
	requireFFmpeg(t)
	r := newTestRunner(t, 1)
	dir := t.TempDir()
	base, layer, out := filepath.Join(dir, "base.mp4"), filepath.Join(dir, "alpha.webm"), filepath.Join(dir, "out.mp4")
	makeColorClip(t, r, base, "white", 2)
	if err := r.run(context.Background(), "-y", "-f", "lavfi", "-i", "color=c=red@0.0:s=120x160:r=10:d=2,format=rgba,drawbox=x=30:y=50:w=40:h=70:color=red@1:t=fill:replace=1",
		"-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-auto-alt-ref", "0", layer); err != nil {
		t.Fatal(err)
	}
	if err := r.OverlayPresenter(context.Background(), base, layer, out); err != nil {
		t.Fatal(err)
	}
	p, err := r.Probe(context.Background(), out)
	if err != nil {
		t.Fatal(err)
	}
	if p.DurationSec < 1.9 || p.DurationSec > 2.1 {
		t.Fatalf("duration changed: %+v", p)
	}
	if info, err := os.Stat(out); err != nil || info.Size() < 1000 {
		t.Fatal("output missing")
	}
	// Transparent background must not overwrite the white source in the content lane.
	before, after := frameStatsAt(t, base, 1), frameStatsAt(t, out, 1)
	if after["YAVG"] < before["YAVG"]*.45 {
		t.Fatalf("alpha or content lost: %v -> %v", before, after)
	}
}

func TestImportedLive2DLayerComposesWhenProvided(t *testing.T) {
	layer := os.Getenv("SCID_TEST_LIVE2D_LAYER")
	if layer == "" {
		t.Skip("opt-in real Cubism layer")
	}
	r := newTestRunner(t, 1)
	dir := t.TempDir()
	base := filepath.Join(dir, "base.mp4")
	makeColorClip(t, r, base, "white", 3)
	if err := r.OverlayPresenter(context.Background(), base, layer, filepath.Join(dir, "final.mp4")); err != nil {
		t.Fatal(err)
	}
}
