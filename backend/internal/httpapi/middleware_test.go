package httpapi

// 本文件覆盖 CORS 中间件的**来源判定**。
//
// 与 `internal/ws` 的 `CheckOrigin` 是同一个坑的两处实现：两者都曾把 `*`
// 当成普通字面来源比对，于是 `SCID_CORS_ALLOWED_ORIGINS=*` 这种
// 「全放开」的写法反而**一个 CORS 头都不返回**。
//
// 这个缺陷在本机联调时被掩盖了：前端走 Vite 代理，浏览器看来是同源请求，
// 压根不需要 CORS。但真正跨域部署（前端与网关不同源）时，
// 表现会是「接口全部失败，而服务端日志里 200 一片正常」。

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
)

func corsProbe(t *testing.T, allowed []string, origin string) *httptest.ResponseRecorder {
	t.Helper()
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(CORSMiddleware(allowed))
	r.GET("/probe", func(c *gin.Context) { c.String(http.StatusOK, "ok") })

	req := httptest.NewRequest(http.MethodGet, "/probe", nil)
	if origin != "" {
		req.Header.Set("Origin", origin)
	}
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	return w
}

// TestCORSWildcardAllowsAnyOrigin 是核心回归：`*` 必须等同于放开。
func TestCORSWildcardAllowsAnyOrigin(t *testing.T) {
	const origin = "http://127.0.0.1:5173"
	w := corsProbe(t, []string{"*"}, origin)

	if got := w.Header().Get("Access-Control-Allow-Origin"); got != origin {
		t.Errorf("配了 `*` 时应当回显来源 %q，实际 %q —— 跨域部署下所有接口都会失败",
			origin, got)
	}
	// 必须回显具体来源而不是字面 `*`，否则与 Allow-Credentials 冲突，
	// 浏览器会直接丢弃这个响应。
	if got := w.Header().Get("Access-Control-Allow-Credentials"); got != "true" {
		t.Errorf("Allow-Credentials 期望 true，实际 %q", got)
	}
}

// TestCORSExplicitListIsEnforced 反向对照：显式白名单之外的来源不能拿到 CORS 头。
func TestCORSExplicitListIsEnforced(t *testing.T) {
	allowed := []string{"https://app.example.com"}

	if got := corsProbe(t, allowed, "https://app.example.com").
		Header().Get("Access-Control-Allow-Origin"); got != "https://app.example.com" {
		t.Errorf("白名单内的来源应当被允许，实际 %q", got)
	}
	if got := corsProbe(t, allowed, "https://evil.example").
		Header().Get("Access-Control-Allow-Origin"); got != "" {
		t.Errorf("白名单外的来源不应拿到 Allow-Origin，实际 %q", got)
	}
}

// TestCORSEmptyListAllowsEveryOrigin 钉住「空列表 = 放开」的原有语义。
func TestCORSEmptyListAllowsEveryOrigin(t *testing.T) {
	const origin = "https://anything.example"
	if got := corsProbe(t, nil, origin).Header().Get("Access-Control-Allow-Origin"); got != origin {
		t.Errorf("空允许列表应当放开所有来源，实际 %q", got)
	}
}

// TestCORSPreflightBypassesHandler 预检请求必须直接 204，不进业务 handler。
func TestCORSPreflightBypassesHandler(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(CORSMiddleware([]string{"*"}))
	called := false
	r.OPTIONS("/probe", func(c *gin.Context) { called = true })

	req := httptest.NewRequest(http.MethodOptions, "/probe", nil)
	req.Header.Set("Origin", "http://127.0.0.1:5173")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusNoContent {
		t.Errorf("预检期望 204，实际 %d", w.Code)
	}
	if called {
		t.Error("预检请求不应进入业务 handler")
	}
}
