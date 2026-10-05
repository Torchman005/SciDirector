package media

import (
	"context"
	"fmt"
	"math"
	"os/exec"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"
)

func TestCanParallelTransition(t *testing.T) {
	plan := PlanTransitions([]float64{2, 2, 2}, TransitionSpec{Type: TransitionFade, DurationSec: 0.4})
	if !CanParallelTransition([]float64{2, 2, 2}, plan, 15) {
		t.Fatal("ordinary transition should allow parallel pieces")
	}
	short := PlanTransitions([]float64{1, 0.4, 1}, TransitionSpec{Type: TransitionFade, DurationSec: 0.4})
	if CanParallelTransition([]float64{1, 0.4, 1}, short, 15) {
		t.Fatal("a shot entirely covered by transitions needs serial fallback")
	}
	two := PlanTransitions([]float64{2, 2}, TransitionSpec{Type: TransitionFade, DurationSec: 0.4})
	if CanParallelTransition([]float64{2, 2}, two, 15) {
		t.Fatal("two clips do not amortize extra ffmpeg launches")
	}
}

func TestParallelTransitionPreservesTimeline(t *testing.T) {
	requireFFmpeg(t)
	r := newTestRunner(t, 3)
	ctx := context.Background()
	dir := t.TempDir()
	inputs := make([]string, 3)
	for i := range inputs {
		inputs[i] = filepath.Join(dir, fmt.Sprintf("input_%d.mp4", i))
		color := []string{"red", "green", "blue"}[i]
		if err := r.run(ctx, "-hide_banner", "-nostdin", "-y",
			"-f", "lavfi", "-i", fmt.Sprintf("color=c=%s:s=320x240:r=15:d=2", color),
			"-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
			"-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
			"-c:a", "aac", "-shortest", inputs[i]); err != nil {
			t.Fatal(err)
		}
	}
	durations := []float64{2, 2, 2}
	plan := PlanTransitions(durations, TransitionSpec{Type: TransitionFade, DurationSec: 0.4})
	out := filepath.Join(dir, "parallel.mp4")
	if err := r.ConcatWithTransitionParallel(ctx, inputs, durations, out, plan, 3); err != nil {
		t.Fatal(err)
	}
	probe, err := r.Probe(ctx, out)
	if err != nil {
		t.Fatal(err)
	}
	if !probe.HasAudio || math.Abs(probe.DurationSec-plan.OutDuration) > 0.15 {
		t.Fatalf("parallel output lost audio or timeline: %+v, expected %.3fs", probe, plan.OutDuration)
	}
}

func TestParallelTransitionFiveClipsKeepsOrderAndDuration(t *testing.T) {
	requireFFmpeg(t)
	r := newTestRunner(t, 4)
	ctx := context.Background()
	dir := t.TempDir()
	colors := []string{"red", "green", "blue", "yellow", "magenta"}
	inputs := make([]string, len(colors))
	durations := make([]float64, len(colors))
	for i, color := range colors {
		inputs[i] = filepath.Join(dir, fmt.Sprintf("shot_%d.mp4", i))
		durations[i] = 2
		if err := r.run(ctx, "-hide_banner", "-nostdin", "-y",
			"-f", "lavfi", "-i", fmt.Sprintf("color=c=%s:s=160x120:r=15:d=2", color),
			"-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
			"-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
			"-c:a", "aac", "-shortest", inputs[i]); err != nil {
			t.Fatal(err)
		}
	}
	plan := PlanTransitions(durations, TransitionSpec{Type: TransitionFade, DurationSec: 0.4})
	parallel := filepath.Join(dir, "parallel.mp4")
	var maxActive atomic.Int32
	monitorDone := make(chan struct{})
	monitorStopped := make(chan struct{})
	go func() {
		defer close(monitorStopped)
		ticker := time.NewTicker(time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-monitorDone:
				return
			case <-ticker.C:
				active := int32(len(r.sem.ch))
				if active > maxActive.Load() {
					maxActive.Store(active)
				}
			}
		}
	}()
	parallelStart := time.Now()
	if err := r.ConcatWithTransitionParallel(ctx, inputs, durations, parallel, plan, 4); err != nil {
		close(monitorDone)
		<-monitorStopped
		t.Fatal(err)
	}
	parallelElapsed := time.Since(parallelStart)
	close(monitorDone)
	<-monitorStopped
	if maxActive.Load() < 2 {
		t.Fatalf("expected simultaneous ffmpeg processes, observed max %d", maxActive.Load())
	}
	t.Logf("five clips: parallel=%s, max active ffmpeg=%d", parallelElapsed, maxActive.Load())
	parallelInfo, err := r.Probe(ctx, parallel)
	if err != nil {
		t.Fatal(err)
	}
	if !parallelInfo.HasAudio || math.Abs(parallelInfo.DurationSec-plan.OutDuration) > 0.15 {
		t.Fatalf("parallel timeline differs from plan: parallel=%+v plan=%+v", parallelInfo, plan)
	}
	wantColors := [][]byte{{255, 0, 0}, {0, 128, 0}, {0, 0, 255}, {255, 255, 0}, {255, 0, 255}}
	for i := range colors {
		// Shot centers are well outside the 0.4s transition windows.
		at := float64(i)*1.6 + 1.0
		got := sampleRGB(t, parallel, at)
		want := wantColors[i]
		for channel := range got {
			if math.Abs(float64(got[channel])-float64(want[channel])) > 20 {
				t.Fatalf("shot %d at %.1fs has wrong color/order: rgb=%v want=%v", i, at, got, want)
			}
		}
	}
}

func sampleRGB(t *testing.T, path string, at float64) []byte {
	t.Helper()
	data, err := exec.Command("ffmpeg", "-v", "error", "-nostdin", "-ss", fmt.Sprintf("%.3f", at),
		"-i", path, "-frames:v", "1", "-vf", "scale=1:1", "-f", "rawvideo",
		"-pix_fmt", "rgb24", "-").Output()
	if err != nil || len(data) != 3 {
		t.Fatalf("sample frame at %.3fs failed: %v, bytes=%d", at, err, len(data))
	}
	return data
}
