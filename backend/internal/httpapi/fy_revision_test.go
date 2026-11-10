package httpapi

import (
	"errors"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
)

func TestCheckFyRevisionBase(t *testing.T) {
	hash := strings.Repeat("a", 64)
	request := fyRevisionPrefix + `{"expect_revision":"` + hash + `","update_objects":[{"id":"rotor","layer":2}]}`
	code := `<script id="scid-fy-data" type="application/json">{"revision":"` + hash + `","object_document":{"version":1}}</script>`
	for _, tc := range []struct {
		name, comment, code string
		want                error
	}{
		{"ordinary repair remains compatible", "make the title larger", "old html", nil},
		{"current exact edit", request, code, nil},
		{"stale edit", strings.Replace(request, hash, strings.Repeat("b", 64), 1), code, errFyRevisionConflict},
		{"missing source", request, "", errFyRevisionConflict},
		{"legacy source", request, strings.Replace(code, `,"object_document":{"version":1}`, "", 1), errFyRevisionConflict},
		{"ambiguous marker", request, code + code, errFyRevisionConflict},
		{"malformed request", fyRevisionPrefix + "{", code, errFyRevisionInvalid},
		{"missing revision", fyRevisionPrefix + "{}", code, errFyRevisionInvalid},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if err := checkFyRevisionBase(tc.comment, tc.code); !errors.Is(err, tc.want) {
				t.Fatalf("got %v, want %v", err, tc.want)
			}
		})
	}
}

func TestFyRevisionErrorMapping(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, tc := range []struct {
		err    error
		status int
	}{{errFyRevisionInvalid, 400}, {errFyRevisionConflict, 409}} {
		w := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(w)
		c.Request = httptest.NewRequest("POST", "/shots/test/reject", nil)
		mapError(c, tc.err)
		if w.Code != tc.status {
			t.Fatalf("got %d, want %d", w.Code, tc.status)
		}
	}
}
