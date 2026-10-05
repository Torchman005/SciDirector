package pbconv

import (
	"reflect"
	"testing"

	"github.com/itJinYu/SciDirector/backend/internal/domain"
)

func TestShotBeatsRoundTrip(t *testing.T) {
	shot := &domain.Shot{ShotID: "job-s000", Beats: []string{"opening", "proof", "result"}}
	got := ShotFromPB(ShotToPB(shot))
	if !reflect.DeepEqual(got.Beats, shot.Beats) {
		t.Fatalf("beats lost across proto round trip: got %v, want %v", got.Beats, shot.Beats)
	}
}
