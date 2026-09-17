package ws

// 本文件覆盖 WebSocket 的**来源校验**。
//
// 这是一处真实缺陷的回归测试：`CheckOrigin` 原先只把**空列表**当成
// 「放开所有来源」，而把 `*` 当成一个普通的字面来源去做 map 比对。
// 于是在 `.env` 里写 `SCID_CORS_ALLOWED_ORIGINS=*`（最自然的「全放开」写法）
// 时，**每一次** WebSocket 升级都会被判为非法来源、返回 403。
//
// 为什么这个缺陷特别难发现：页面并不会白屏。REST 轮询仍在更新数据，
// 审核台看起来只是「有点卡」；而连接指示器虽然显示「重连中…」，
// 也很容易被当成网络抖动。真正的问题是**实时事件通道整个是死的** ——
// C1「实时看到」与 C3「断线后补齐」都无从谈起。
//
// 本包此前没有任何测试文件。

import (
	"io"
	"log/slog"
	"net/http"
	"testing"
)

func newTestHub(allowed []string) *Hub {
	return NewHub(allowed, slog.New(slog.NewTextHandler(io.Discard, nil)))
}

// originAllowed 直接问 CheckOrigin：这是 gorilla/websocket 决定 403 的地方。
func originAllowed(t *testing.T, allowed []string, origin string) bool {
	t.Helper()
	h := newTestHub(allowed)
	req, err := http.NewRequest(http.MethodGet, "/ws/jobs/job-x", nil)
	if err != nil {
		t.Fatalf("构造请求失败: %v", err)
	}
	if origin != "" {
		req.Header.Set("Origin", origin)
	}
	return h.upgrader.CheckOrigin(req)
}

// TestCheckOriginWildcardAllowsEveryOrigin 是核心回归：
// `*` 必须等同于「放开」，而不是「只允许字面量 *」。
func TestCheckOriginWildcardAllowsEveryOrigin(t *testing.T) {
	for _, origin := range []string{
		"http://127.0.0.1:5173",
		"http://localhost:5173",
		"https://studio.example.com",
		"http://192.168.1.10:3000",
	} {
		if !originAllowed(t, []string{"*"}, origin) {
			t.Errorf("配了 `*` 时来源 %q 应当被允许 —— 否则 WebSocket 全部 403，"+
				"前端会永远停在「重连中」而页面看起来只是有点卡", origin)
		}
	}
}

// TestCheckOriginEmptyListAllowsEveryOrigin 钉住原有的「空列表 = 放开」语义。
func TestCheckOriginEmptyListAllowsEveryOrigin(t *testing.T) {
	for _, allowed := range [][]string{nil, {}} {
		if !originAllowed(t, allowed, "http://anything.example") {
			t.Errorf("空允许列表应当放开所有来源（allowed=%v）", allowed)
		}
	}
}

// TestCheckOriginExplicitListIsEnforced 反向对照：
// 一旦显式列出来源，就必须**只**允许列出的那些 ——
// 否则「配了白名单」会变成一种安全的错觉。
func TestCheckOriginExplicitListIsEnforced(t *testing.T) {
	allowed := []string{"http://127.0.0.1:5173"}

	if !originAllowed(t, allowed, "http://127.0.0.1:5173") {
		t.Error("列表里的来源应当被允许")
	}
	if originAllowed(t, allowed, "http://evil.example") {
		t.Error("列表外的来源必须被拒绝，否则白名单形同虚设")
	}
}

// TestCheckOriginTrimsWhitespace 覆盖配置里的多余空格。
//
// 逗号分隔的环境变量很容易带上空格（`a, b`），若不 trim，
// 第二个来源会静默失效 —— 又是一个「配了但没生效」。
func TestCheckOriginTrimsWhitespace(t *testing.T) {
	allowed := []string{"  http://127.0.0.1:5173  ", ""}
	if !originAllowed(t, allowed, "http://127.0.0.1:5173") {
		t.Error("带空格的来源项应当被 trim 后匹配")
	}
}
