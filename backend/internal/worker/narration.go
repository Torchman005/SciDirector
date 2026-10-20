package worker

import (
	"fmt"

	"github.com/itJinYu/SciDirector/backend/internal/domain"
	pb "github.com/itJinYu/SciDirector/backend/internal/pb/scidirector/v1"
	"github.com/itJinYu/SciDirector/backend/internal/pbconv"
)

func applyNarrationArtifact(shot *domain.Shot, spec *pb.ShotSpec, artifact *domain.Artifact, partial bool) error {
	if spec == nil || len(spec.NarrationRevisions) == 0 {
		return nil
	}
	if spec.Narration != shot.Narration {
		if partial || artifact == nil || artifact.VideoPath == "" {
			return fmt.Errorf("旁白修订必须与完整的新镜头产物一起发布")
		}
		if old := shot.Artifact; old != nil {
			if artifact.VideoPath == old.VideoPath ||
				(old.AudioPath != "" && (artifact.AudioPath == "" || artifact.AudioPath == old.AudioPath)) {
				return fmt.Errorf("旁白修订不能复用旧视频或旧配音")
			}
		}
	}
	return shot.ApplyNarrationRevision(pbconv.ShotFromPB(spec))
}
