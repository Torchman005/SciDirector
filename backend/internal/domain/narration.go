package domain

import (
	"fmt"
	"reflect"
	"strings"
)

// NarrationRevision retains the full script before/after a verified minimal edit.
type NarrationRevision struct {
	Before  string   `json:"before"`
	After   string   `json:"after"`
	Reason  string   `json:"reason"`
	TaskIDs []string `json:"task_ids"`
	Attempt int      `json:"attempt"`
}

// ApplyNarrationRevision updates semantic fields only for a consistent append-only history.
// Callers must publish it together with its newly rendered artifact, never at code generation.
func (s *Shot) ApplyNarrationRevision(next *Shot) error {
	if next == nil || len(next.NarrationRevisions) == 0 {
		return nil
	}
	if next.ShotID != s.ShotID || len(next.NarrationRevisions) < len(s.NarrationRevisions) {
		return fmt.Errorf("旁白修订的镜头或历史版本不匹配")
	}
	for i, old := range s.NarrationRevisions {
		if !reflect.DeepEqual(old, next.NarrationRevisions[i]) {
			return fmt.Errorf("旁白修订不能覆盖旧版记录")
		}
	}
	current := s.Narration
	for _, revision := range next.NarrationRevisions[len(s.NarrationRevisions):] {
		if revision.Before != current || strings.TrimSpace(revision.After) == "" || revision.After == current ||
			revision.Attempt < 1 || len(revision.TaskIDs) == 0 || strings.TrimSpace(revision.Reason) == "" {
			return fmt.Errorf("旁白修订缺少原文、任务或独立核对依据")
		}
		current = revision.After
	}
	if current != next.Narration {
		return fmt.Errorf("旁白与修订后的文本不一致")
	}
	s.Narration, s.VisualBrief = next.Narration, next.VisualBrief
	s.Beats, s.Keywords = next.Beats, next.Keywords
	s.NarrationRevisions = append([]NarrationRevision(nil), next.NarrationRevisions...)
	return nil
}
