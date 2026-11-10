package worker

import (
	"reflect"
	"time"

	"github.com/itJinYu/SciDirector/backend/internal/domain"
)

// Compare under the store lock, after rendering, without treating our own
// GENERATING transition as a competing edit.
func fyRevisionBaseUnchanged(current, base *domain.Job, shotID string) bool {
	if !reflect.DeepEqual(current.StyleGuide, base.StyleGuide) || !reflect.DeepEqual(current.Effects, base.Effects) {
		return false
	}
	a, b := current.FindShot(shotID), base.FindShot(shotID)
	if a == nil || b == nil || a.Status != domain.StatusGenerating {
		return false
	}
	x, y := *a, *b
	x.Status, y.Status = "", ""
	x.Error, y.Error = "", ""
	x.UpdatedAt, y.UpdatedAt = time.Time{}, time.Time{}
	return reflect.DeepEqual(x, y)
}
