package media

import "math"

// NarrationSafeDuration protects every spoken word, including the time lost to
// the outgoing xfade. Frame rounding prevents a fractional last word being cut.
func NarrationSafeDuration(planned, actual, speech float64, transition TransitionSpec, outgoing bool, fps int) float64 {
	base := planned
	if base <= 0 {
		base = actual
	}
	if speech <= 0 {
		return base
	}
	margin := 0.18
	if outgoing && transition.Type != "" && transition.Type != TransitionNone && transition.DurationSec >= minTransitionSec {
		margin += transition.DurationSec
	}
	target := math.Max(base, speech+margin)
	if fps > 0 {
		target = math.Ceil(target*float64(fps)) / float64(fps)
	}
	return target
}
