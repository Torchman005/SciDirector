package media

import (
	"context"
	"fmt"
)

// PresenterContentScale leaves a 24% lane without cropping scientific diagrams.
const PresenterContentScale = .76

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
	return even(float64(width) * PresenterContentScale), even(float64(height) * PresenterContentScale), even(float64(width) * .22), even(float64(height) * .5)
}

// OverlayPresenter preserves the entire scientific diagram and keeps captions above the avatar.
func (r *Runner) OverlayPresenter(ctx context.Context, base, layer, out string) error {
	probe, err := r.Probe(ctx, base)
	if err != nil {
		return err
	}
	cw, ch, aw, ah := PresenterGeometry(probe.Width, probe.Height)
	filter := fmt.Sprintf("[0:v]scale=%d:%d,pad=%d:%d:0:(oh-ih)/2:color=0x0B1020[base];[1:v]scale=%d:%d[avatar];[base][avatar]overlay=x=W-w-W*0.015:y=H-h-H*0.14:eof_action=pass:format=auto[v]", cw, ch, probe.Width, probe.Height, aw, ah)
	// Native VP9 decoding may drop the alpha plane; explicitly use libvpx for this input.
	return r.run(ctx, "-hide_banner", "-nostdin", "-y", "-i", base, "-c:v", "libvpx-vp9", "-i", layer,
		"-filter_complex", filter, "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
		"-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy", "-t", fmt.Sprintf("%.6f", probe.DurationSec), "-movflags", "+faststart", out)
}
