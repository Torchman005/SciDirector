package httpapi

import (
	"encoding/json"
	"errors"
	"regexp"
	"strings"
)

const fyRevisionPrefix = "SCID_FY_REVISION_V1:"

var (
	errFyRevisionInvalid  = errors.New("对象修订格式无效，请重新打开对象工作台")
	errFyRevisionConflict = errors.New("镜头版本已更新或不支持对象编辑，请刷新后重新编辑")
	fyRevisionHash        = regexp.MustCompile(`^[0-9a-f]{64}$`)
	fyDataMarker          = regexp.MustCompile(`(?s)<script id="scid-fy-data" type="application/json">(.*?)</script>`)
)

// checkFyRevisionBase rejects stale edits before changing state or consuming an
// attempt. Python still validates the full patch, content hash and render context.
func checkFyRevisionBase(comment, code string) error {
	if !strings.HasPrefix(comment, fyRevisionPrefix) {
		return nil
	}
	var request struct {
		ExpectRevision string `json:"expect_revision"`
	}
	if json.Unmarshal([]byte(strings.TrimPrefix(comment, fyRevisionPrefix)), &request) != nil || !fyRevisionHash.MatchString(request.ExpectRevision) {
		return errFyRevisionInvalid
	}
	matches := fyDataMarker.FindAllStringSubmatch(code, -1)
	if len(matches) != 1 {
		return errFyRevisionConflict
	}
	var data struct {
		Revision string           `json:"revision"`
		Document *json.RawMessage `json:"object_document"`
	}
	if json.Unmarshal([]byte(matches[0][1]), &data) != nil || data.Document == nil || data.Revision != request.ExpectRevision {
		return errFyRevisionConflict
	}
	return nil
}
