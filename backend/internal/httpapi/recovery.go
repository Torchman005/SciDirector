package httpapi

import (
	"errors"
	"strings"

	"github.com/itJinYu/SciDirector/backend/internal/domain"
)

const automaticRepairComment = "自动诊断并修复：根据系统错误、源码及审核记录定位问题，修复后重新渲染并审核。"

var errRepairBusy = errors.New("镜头正在处理中，请等待本次修复结束")

func repairComment(comment string) string {
	if comment = strings.TrimSpace(comment); comment == "" {
		return automaticRepairComment
	}
	return comment
}

func canRequestRepair(status domain.ShotStatus) bool {
	// Reject duplicate clicks/races even though the generic transition helper
	// deliberately accepts same-status writes for event replay.
	switch status {
	case domain.StatusAwaitingHuman, domain.StatusApproved, domain.StatusFailed, domain.StatusRejected:
		return true
	}
	return false
}
