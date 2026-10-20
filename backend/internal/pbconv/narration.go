package pbconv

import (
	"github.com/itJinYu/SciDirector/backend/internal/domain"
	pb "github.com/itJinYu/SciDirector/backend/internal/pb/scidirector/v1"
)

func narrationRevisionsToPB(rows []domain.NarrationRevision) []*pb.NarrationRevision {
	var result []*pb.NarrationRevision
	for _, row := range rows {
		result = append(result, &pb.NarrationRevision{Before: row.Before, After: row.After,
			Reason: row.Reason, TaskIds: row.TaskIDs, Attempt: int32(row.Attempt)})
	}
	return result
}

func narrationRevisionsFromPB(rows []*pb.NarrationRevision) []domain.NarrationRevision {
	var result []domain.NarrationRevision
	for _, row := range rows {
		if row != nil {
			result = append(result, domain.NarrationRevision{Before: row.Before, After: row.After,
				Reason: row.Reason, TaskIDs: row.TaskIds, Attempt: int(row.Attempt)})
		}
	}
	return result
}
