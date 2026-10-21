package media

// Encoding profiles.
//
// A shot passes through several re-encodes before delivery (normalize →
// transitions → presenter overlay → grade/subtitles → final mux). At crf 20
// every generation visibly softened thin connector lines and text edges and the
// final file averaged ~480 kb/s at 1080p. Intermediates are therefore encoded
// near-lossless and fast; exactly one delivery encode (MuxFinal) spends the
// time on a slow, animation-tuned, rate-capped encode.

// IntermediateCRF is used by every encode whose output is re-encoded later.
const IntermediateCRF = "12"

// intermediateVideoArgs is the fast near-lossless profile for working files.
func intermediateVideoArgs() []string {
	return []string{"-c:v", "libx264", "-preset", "veryfast", "-crf", IntermediateCRF}
}

// deliveryVideoArgs is the single, final encode that produces final.mp4.
func deliveryVideoArgs() []string {
	return []string{
		"-c:v", "libx264", "-preset", "slow", "-crf", "16", "-tune", "animation",
		"-maxrate", "12M", "-bufsize", "24M", "-pix_fmt", "yuv420p",
		"-color_range", "tv",
		"-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
	}
}

// scaleFlags keeps text and 1-2px strokes crisp when resampling.
const scaleFlags = ":flags=lanczos"
