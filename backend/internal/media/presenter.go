package media

import (
	"context"
	"fmt"
)

// PresenterContentScale preserves the full-frame scientific animation.
const PresenterContentScale = 1.0

// PresenterSafeArea includes motion clearance around the bottom-right overlay.
// Keep the Python layout contract and these normalized bounds in sync.
func PresenterSafeArea() map[string]float64 {
	return map[string]float64{"x": .78, "y": .55, "width": .22, "height": .45}
}

// PresenterGeometry reserves a right-hand lane and the lower subtitle strip.
// Even rounding keeps H.264/YUV420 dimensions legal at any output resolution.
func PresenterGeometry(width, height int) (contentWidth, contentHeight, avatarWidth, avatarHeight int) {
	even := func(value float64) int {
		n := int(value) / 2 * 2
		if n < 2 {
			return 2
		}
		return n
	}
	return even(float64(width)), even(float64(height)), even(float64(width) * .20), even(float64(height) * .42)
}

// OverlayPresenter preserves the entire scientific diagram and keeps captions above the avatar.
func (r *Runner) OverlayPresenter(ctx context.Context, base, layer, out string) error {
	probe, err := r.Probe(ctx, base)
	if err != nil {
		return err
	}
	_, _, aw, ah := PresenterGeometry(probe.Width, probe.Height)
	filter := fmt.Sprintf("[1:v]scale=%d:%d[avatar];[0:v][avatar]overlay=x=W-w-W*0.006:y=H-h-H*0.006:eof_action=pass:format=auto[v]", aw, ah)
	// Native VP9 decoding may drop the alpha plane; explicitly use libvpx for this input.
	return r.run(ctx, "-hide_banner", "-nostdin", "-y", "-i", base, "-c:v", "libvpx-vp9", "-i", layer,
		"-filter_complex", filter, "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
		"-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy", "-t", fmt.Sprintf("%.6f", probe.DurationSec), "-movflags", "+faststart", out)
}
