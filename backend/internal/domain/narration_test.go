package domain

import (
	"encoding/json"
	"reflect"
	"testing"
)

func TestNarrationRevisionRequiresConsistentHistoryAndKeepsJobSource(t *testing.T) {
	for _, invalid := range []string{"", "source", "target", "history"} {
		t.Run(invalid, func(t *testing.T) {
			shot := &Shot{ShotID: "s", Narration: "original", VisualBrief: "old sketch", Beats: []string{"old"}}
			next := &Shot{ShotID: "s", Narration: "corrected", VisualBrief: "correct sketch",
				NarrationRevisions: []NarrationRevision{{Before: "original", After: "corrected", Reason: "independently verified", TaskIDs: []string{"r1"}, Attempt: 2}}}
			switch invalid {
			case "source":
				next.NarrationRevisions[0].Before = "not original"
			case "target":
				next.Narration = "unrecorded text"
			case "history":
				next.NarrationRevisions[0].Reason = ""
			}
			job := &Job{RawScript: "user's original source", Shots: []*Shot{shot}}
			err := shot.ApplyNarrationRevision(next)
			if invalid != "" {
				if err == nil || shot.Narration != "original" || len(shot.NarrationRevisions) != 0 {
					t.Fatalf("invalid update applied: %+v", shot)
				}
				return
			}
			if err != nil || job.RawScript != "user's original source" || shot.Narration != "corrected" || len(shot.Beats) != 0 {
				t.Fatalf("incorrect semantic update: %+v, %v", shot, err)
			}
			if err := shot.ApplyNarrationRevision(next); err != nil {
				t.Fatal("replay must be idempotent", err)
			}
			raw, _ := json.Marshal(job)
			var restored Job
			if err := json.Unmarshal(raw, &restored); err != nil || !reflect.DeepEqual(restored.Shots[0].NarrationRevisions, shot.NarrationRevisions) {
				t.Fatal("history lost on persistence", err)
			}
			corrupt := *next
			corrupt.NarrationRevisions = append([]NarrationRevision(nil), next.NarrationRevisions...)
			corrupt.NarrationRevisions[0].Reason = "rewritten history"
			if err := shot.ApplyNarrationRevision(&corrupt); err == nil {
				t.Fatal("historical edit accepted")
			}
		})
	}
}
