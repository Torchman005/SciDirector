package httpapi

import (
	"net/http"
	"strings"
	"testing"
)

func TestKnownModelFaultRejectsGenerationBeforeEnqueue(t *testing.T) {
	for _, role := range []string{"text", "vision"} {
		t.Run(role, func(t *testing.T) {
			h := newHarnessWithQuota(t, 0, "model:"+role+"=unavailable:provider/model HTTP 404")
			w := h.asTenant(http.MethodPost, "/api/v1/generate", tenantA, `{"raw_script":"讲解光的反射定律，演示入射光线与反射光线的角度关系。","target_duration_sec":30}`)
			if w.Code != 503 || !strings.Contains(w.Body.String(), "模型配置不可用") {
				t.Fatalf("%d %s", w.Code, w.Body.String())
			}
		})
	}
}
