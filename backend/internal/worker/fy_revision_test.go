package worker

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/itJinYu/SciDirector/backend/internal/domain"
)

func TestFyRevisionRejectsChangedPublicationBase(t *testing.T) {
	base := &domain.Job{StyleGuide: map[string]any{"animation_style": "fy"}, Shots: []*domain.Shot{{
		ShotID: "s1", Code: "original", Narration: "original words", Attempt: 1,
		Status: domain.StatusRetrying, Error: "old failure", Artifact: &domain.Artifact{ArtifactID: "v1"},
	}}}
	for _, change := range []string{"none", "code", "narration", "artifact", "style", "attempt", "status", "ledger"} {
		t.Run(change, func(t *testing.T) {
			data, _ := json.Marshal(base)
			var current domain.Job
			if err := json.Unmarshal(data, &current); err != nil {
				t.Fatal(err)
			}
			s := current.Shots[0]
			s.Status, s.Error, s.UpdatedAt = domain.StatusGenerating, "", time.Now()
			switch change {
			case "code":
				s.Code = "other edit"
			case "narration":
				s.Narration = "other words"
			case "artifact":
				s.Artifact.ArtifactID = "v2"
			case "style":
				current.StyleGuide["animation_style"] = "other"
			case "attempt":
				s.Attempt++
			case "status":
				s.Status = domain.StatusApproved
			case "ledger":
				s.RepairTasks = []domain.RepairTask{{TaskID: "new-task"}}
			}
			if got := fyRevisionBaseUnchanged(&current, base, "s1"); got != (change == "none") {
				t.Fatalf("publication match = %v for %s", got, change)
			}
		})
	}
}
