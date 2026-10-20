package domain

import "strings"

const (
	// Feedback kinds distinguish review decisions from unscored operations and faults.
	FeedbackContentReview     = "content_review"
	FeedbackScienceReview     = "science_review"
	FeedbackRepairRequest     = "repair_request"
	FeedbackHumanReview       = "human_review"
	FeedbackReviewUnavailable = "review_unavailable"
	FeedbackSystemError       = "system_error"
)

// ReviewKind reads legacy records without rewriting their history.
func (f Feedback) ReviewKind() string {
	if f.Kind != "" {
		return f.Kind
	}
	if f.Source == FeedbackVLM {
		return FeedbackContentReview
	}
	if f.Source == FeedbackHuman {
		if !f.Passed && len(f.Issues) == 1 && strings.HasPrefix(f.Issues[0], "自动诊断并修复：") {
			return FeedbackRepairRequest
		}
		return FeedbackHumanReview
	}
	if strings.HasPrefix(f.Model, "science:") {
		return FeedbackScienceReview
	}
	for _, issue := range f.Issues {
		for _, prefix := range []string{"VLM 审查调用失败", "科学源码核对未完成", "审核意见定位补充失败", "审核意见缺少有效定位", "渲染产物没有可用的抽帧"} {
			if strings.HasPrefix(issue, prefix) {
				return FeedbackReviewUnavailable
			}
		}
	}
	return FeedbackSystemError
}

// RepairLedger reconstructs old jobs from their immutable review snapshots.
func (s *Shot) RepairLedger() []RepairTask {
	if s.RepairTasks != nil {
		return s.RepairTasks
	}
	var ledger []RepairTask
	for _, f := range s.Feedbacks {
		kind := f.ReviewKind()
		if kind == FeedbackContentReview || kind == FeedbackScienceReview {
			ledger = mergeRepairLedger(ledger, f.RepairTasks, true)
		} else if kind == FeedbackReviewUnavailable {
			ledger = mergeRepairLedger(ledger, f.RepairTasks, false)
		}
	}
	return ledger
}

// RecordFeedback keeps the current task ledger independent from requests/errors.
// Only a completed review may update resolution status; missing tasks remain open.
func (s *Shot) RecordFeedback(f Feedback) {
	f.Kind = f.ReviewKind()
	if f.ArtifactID == "" && s.Artifact != nil {
		f.ArtifactID = s.Artifact.ArtifactID
	}
	ledger := s.RepairLedger()
	canResolve := f.Kind == FeedbackContentReview || f.Kind == FeedbackScienceReview
	if !canResolve && f.Kind != FeedbackHumanReview {
		f.Passed = false
	}
	if canResolve || f.Kind == FeedbackReviewUnavailable {
		ledger = mergeRepairLedger(ledger, f.RepairTasks, canResolve)
		f.RepairTasks = append([]RepairTask(nil), ledger...)
		for _, task := range ledger {
			if task.Severity != "advisory" && task.Status != "resolved" {
				f.Passed = false
				if len(f.Suggestions) == 0 && canResolve {
					f.Suggestions = append(f.Suggestions, task.Instruction)
				}
			}
		}
	}
	s.RepairTasks = append([]RepairTask(nil), ledger...)
	s.Feedbacks = append(s.Feedbacks, f)
}

func mergeRepairLedger(old, incoming []RepairTask, canResolve bool) []RepairTask {
	ledger := append([]RepairTask(nil), old...)
	for _, task := range incoming {
		index := -1
		for i := range ledger {
			if ledger[i].TaskID == task.TaskID {
				index = i
				break
			}
		}
		if index < 0 {
			ledger = append(ledger, task)
			continue
		}
		// Original location, evidence, target and acceptance are permanent.
		if canResolve && (task.Status != "resolved" || task.ResolutionEvidence != "") {
			ledger[index].Status = task.Status
			ledger[index].ResolutionEvidence = task.ResolutionEvidence
		}
		if len(task.EvidencePairs) > 0 {
			ledger[index].EvidencePairs = task.EvidencePairs
		}
	}
	return ledger
}

// RepairFeedback forwards the latest content decision with the durable ledger.
// A later outage or repair click must not replace concrete repair instructions.
func (s *Shot) RepairFeedback() *Feedback {
	for i := len(s.Feedbacks) - 1; i >= 0; i-- {
		f := s.Feedbacks[i]
		if kind := f.ReviewKind(); kind != FeedbackContentReview && kind != FeedbackScienceReview {
			continue
		}
		f.RepairTasks = append([]RepairTask(nil), s.RepairLedger()...)
		for _, task := range f.RepairTasks {
			if task.Severity != "advisory" && task.Status != "resolved" {
				f.Passed = false
				if len(f.Suggestions) == 0 {
					f.Suggestions = append(f.Suggestions, task.Instruction)
				}
			}
		}
		return &f
	}
	if ledger := s.RepairLedger(); len(ledger) > 0 {
		return &Feedback{Kind: FeedbackReviewUnavailable, Source: FeedbackSystem, RepairTasks: ledger,
			Message: "已保留历史修复任务，尚无可用的完整内容审查。"}
	}
	return nil
}
