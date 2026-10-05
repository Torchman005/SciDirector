package media

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
)

// ConcatWithTransitionParallel encodes independent shot bodies and transition
// windows concurrently, then stream-copies the ordered pieces into one movie.
// Each source frame belongs to exactly one piece, so the output keeps the same
// timeline as the single-filter xfade path without serially encoding the film.
func (r *Runner) ConcatWithTransitionParallel(
	ctx context.Context, inputs []string, durations []float64, out string,
	plan TransitionPlan, parallel int,
) error {
	if !plan.Enabled || len(inputs) < 2 || len(inputs) != len(durations) {
		return fmt.Errorf("media: invalid parallel transition inputs")
	}
	workDir := filepath.Join(filepath.Dir(out), "transition_pieces")
	if err := os.MkdirAll(workDir, 0o755); err != nil {
		return err
	}
	paths := make([]string, len(inputs)*2-1)
	pool := NewPool(parallel)
	err := pool.Run(ctx, len(paths), func(ctx context.Context, piece int) error {
		path := filepath.Join(workDir, fmt.Sprintf("piece_%03d.mp4", piece))
		paths[piece] = path
		if piece%2 == 0 {
			i := piece / 2
			start := 0.0
			if i > 0 {
				start = plan.Duration
			}
			end := durations[i]
			if i < len(inputs)-1 {
				end -= plan.Duration
			}
			if end <= start {
				return fmt.Errorf("media: shot %d has no body after transition", i)
			}
			return r.encodeTransitionBody(ctx, inputs[i], path, start, end-start)
		}
		i := piece / 2
		return r.encodeTransitionWindow(ctx, inputs[i], inputs[i+1], path,
			durations[i]-plan.Duration, plan)
	})
	if err != nil {
		return err
	}
	list := filepath.Join(workDir, "pieces.txt")
	if err := WriteConcatList(list, paths); err != nil {
		return err
	}
	return r.Concat(ctx, list, out)
}

// CanParallelTransition requires enough independent work to amortize the extra
// ffmpeg launches, and at least one frame in every body.
func CanParallelTransition(durations []float64, plan TransitionPlan, fps int) bool {
	if !plan.Enabled || len(durations) < 3 || fps <= 0 {
		return false
	}
	for i, duration := range durations {
		body := duration
		if i > 0 {
			body -= plan.Duration
		}
		if i < len(durations)-1 {
			body -= plan.Duration
		}
		if body < 1/float64(fps) {
			return false
		}
	}
	return true
}

func (r *Runner) encodeTransitionBody(ctx context.Context, in, out string, start, duration float64) error {
	filter := fmt.Sprintf(
		"[0:v]trim=start=%.6f:duration=%.6f,setpts=PTS-STARTPTS,format=yuv420p[vout];"+
			"[0:a]atrim=start=%.6f:duration=%.6f,asetpts=PTS-STARTPTS[aout]",
		start, duration, start, duration,
	)
	return r.run(ctx, append([]string{"-hide_banner", "-nostdin", "-y", "-i", in,
		"-filter_complex_threads", "1", "-filter_complex", filter}, transitionEncodeArgs(out)...)...)
}

func (r *Runner) encodeTransitionWindow(
	ctx context.Context, left, right, out string, leftStart float64, plan TransitionPlan,
) error {
	t := plan.Duration
	filter := fmt.Sprintf(
		"[0:v]trim=start=%.6f:duration=%.6f,setpts=PTS-STARTPTS[v0];"+
			"[1:v]trim=duration=%.6f,setpts=PTS-STARTPTS[v1];"+
			"[v0][v1]xfade=transition=%s:duration=%.6f:offset=0,format=yuv420p[vout];"+
			"[0:a]atrim=start=%.6f:duration=%.6f,asetpts=PTS-STARTPTS[a0];"+
			"[1:a]atrim=duration=%.6f,asetpts=PTS-STARTPTS[a1];"+
			"[a0][a1]acrossfade=d=%.6f:c1=tri:c2=tri[aout]",
		leftStart, t, t, plan.Type, t, leftStart, t, t, t,
	)
	return r.run(ctx, append([]string{"-hide_banner", "-nostdin", "-y", "-i", left,
		"-i", right, "-filter_complex_threads", "1", "-filter_complex", filter}, transitionEncodeArgs(out)...)...)
}

func transitionEncodeArgs(out string) []string {
	return []string{
		"-map", "[vout]", "-map", "[aout]",
		// Several encoders run at once. Bound threads per encoder so they do not
		// each claim every CPU and turn process parallelism into contention.
		"-c:v", "libx264", "-threads:v", "2", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
		"-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
		"-color_range", "tv", "-colorspace", "bt709",
		"-color_primaries", "bt709", "-color_trc", "bt709",
		// These are temporary pieces. The final Concat applies faststart once.
		out,
	}
}
