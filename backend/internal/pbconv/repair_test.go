package pbconv

import (
	"reflect"
	"testing"

	"github.com/itJinYu/SciDirector/backend/internal/domain"
)

func TestRepairTasksRoundTrip(t *testing.T) {
	feedback := &domain.Feedback{Passed: false, Suggestions: []string{"separate labels"},
		FatalIssues: []string{"formula error"}, RepairTasks: []domain.RepairTask{{
			TaskID: "r1-01", Category: "logic", Severity: "blocking", StartSec: 1, EndSec: 3,
			FrameIndices: []int32{2}, Target: "formula", Evidence: "wrong operator",
			Instruction: "correct operator", Acceptance: "formula matches narration",
			Region: []float64{0.1, 0.2, 0.3, 0.4}, Status: "open",
		}}}
	back := FeedbackFromPB(FeedbackToPB(feedback))
	if !reflect.DeepEqual(back.RepairTasks, feedback.RepairTasks) || !reflect.DeepEqual(back.FatalIssues, feedback.FatalIssues) {
		t.Fatalf("repair evidence lost: %+v", back)
	}
}
