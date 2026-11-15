package access

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// TestLockedPageServesSteamRecoveryUI P0 找回回归：锁定页必须内联 Steam
// 找回 UI（五个公开端点的调用面齐全）且局域网解锁表单不受影响。
func TestLockedPageServesSteamRecoveryUI(t *testing.T) {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/dashboard", nil)
	serveLockedPage(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("锁定页状态码 = %d", rec.Code)
	}
	body := rec.Body.String()
	for _, marker := range []string{
		"/api/v1/session/recovery/login/start",
		"/api/v1/session/recovery/login/code",
		"/api/v1/session/recovery/login/status",
		"/api/v1/session/recovery/complete",
		"/api/v1/session/recovery/cancel",
		"holdexar-recovery", // 找回视图容器 id
	} {
		if !strings.Contains(body, marker) {
			t.Fatalf("锁定页缺少找回 UI 标记：%s", marker)
		}
	}
	// 局域网解锁表单不受影响
	if !strings.Contains(body, "/api/v1/session/unlock") {
		t.Fatal("锁定页缺少局域网解锁表单")
	}
}
