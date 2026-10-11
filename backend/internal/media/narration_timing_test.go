package media

import (
	"math"
	"testing"
)

func TestNarrationOutlivesPlannedShotWithTransition(t *testing.T) {
	spec := TransitionSpec{Type: TransitionFade, DurationSec: .4}
	d := []float64{NarrationSafeDuration(3, 3, 5, spec, true, 30), NarrationSafeDuration(3, 3, 4, spec, false, 30)}
	w := PlanShotWindows(d, PlanTransitions(d, spec))
	if w[0].Duration() < 5.17 || w[1].Duration() < 4.17 {
		t.Fatalf("speech truncated: %+v", w)
	}
}

func TestWordAlignmentKeepsUnevenSpeechAndScriptPunctuation(t *testing.T) {
	text := "先看 FFN。然后继续。"
	marks := []NarrationMark{{"先看", .1, .5}, {"FFN", .7, 2.0}, {"然后", 3.5, .3}, {"继续", 3.9, .5}}
	cues := PlanCuesWithMarks([]Window{{10, 15}}, []string{text}, []float64{4.5}, [][]NarrationMark{marks}, DefaultSubtitleOptions())
	if len(cues) != 2 || cues[0].Text != "先看 FFN。" || math.Abs(cues[0].End-12.7) > 1e-6 || cues[1].Start != 13.5 {
		t.Fatalf("lost anchors: %+v", cues)
	}
}

func TestStaleMarksWithSameSentenceCountAreRejected(t *testing.T) {
	if cues := planCuesFromMarks(Window{0, 6}, "正确文本。", []NarrationMark{{"旧的文字。", 1, 2}}, DefaultSubtitleOptions()); cues != nil {
		t.Fatal(cues)
	}
}
