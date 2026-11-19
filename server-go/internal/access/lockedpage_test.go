package access

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

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
		"holdexar-recovery",
	} {
		if !strings.Contains(body, marker) {
			t.Fatalf("锁定页缺少找回 UI 标记：%s", marker)
		}
	}
	if !strings.Contains(body, "/api/v1/session/unlock") {
		t.Fatal("锁定页缺少局域网解锁表单")
	}
}

func TestLockedPagePerRequestCSPNonce(t *testing.T) {
	fetch := func() (string, string) {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/", nil)
		serveLockedPage(rec, req)
		if rec.Code != http.StatusOK {
			t.Fatalf("锁定页状态码 = %d", rec.Code)
		}
		return rec.Header().Get("Content-Security-Policy"), rec.Body.String()
	}

	csp1, body1 := fetch()
	csp2, body2 := fetch()
	if csp1 == "" || csp2 == "" || csp1 == csp2 {
		t.Fatalf("每次请求应生成独立 CSP nonce: %q vs %q", csp1, csp2)
	}
	if strings.Contains(csp1, "script-src 'self'") || strings.Contains(csp1, "'unsafe-inline'; connect-src") {
		t.Fatalf("锁定页 script-src 不得回退为 'self' 或 'unsafe-inline': %q", csp1)
	}
	if !strings.Contains(csp1, "img-src 'self' data:") {
		t.Fatalf("锁定页 CSP 需允许内联 data: Logo: %q", csp1)
	}

	const prefix = "script-src 'nonce-"
	idx := strings.Index(csp1, prefix)
	if idx < 0 {
		t.Fatalf("锁定页 CSP 缺少 nonce 指令: %q", csp1)
	}
	rest := csp1[idx+len(prefix):]
	nonce, _, ok := strings.Cut(rest, "'")
	if !ok || nonce == "" {
		t.Fatalf("锁定页 CSP nonce 解析失败: %q", csp1)
	}
	if !strings.Contains(body1, `<script nonce="`+nonce+`">`) {
		t.Fatalf("锁定页内联脚本未使用同一次请求的 CSP nonce=%q", nonce)
	}
	if strings.Contains(body1, "__NONCE__") || strings.Contains(body2, "__NONCE__") {
		t.Fatal("锁定页残留未替换的 nonce 占位符")
	}
}
