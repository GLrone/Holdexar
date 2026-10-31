package systemx

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"holdexar/server-go/internal/access"
)

func sessionMux(t *testing.T, dev bool) (*access.Gate, http.Handler) {
	t.Helper()
	gate := access.New()
	mux := http.NewServeMux()
	RegisterSession(mux, gate, dev)
	return gate, mux
}

func TestBootstrapInvalidNonce(t *testing.T) {
	_, h := sessionMux(t, false)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "http://127.0.0.1:28765/api/v1/session/bootstrap?nonce=bad", nil))
	if rec.Code != 401 {
		t.Fatalf("状态码=%d，应为 401", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "引导票据无效或已过期") {
		t.Fatalf("文案不符: %s", rec.Body.String())
	}
}

func TestBootstrapValidIssuesCookieAndRedirects(t *testing.T) {
	gate, h := sessionMux(t, false)
	nonce := CreateBootstrap()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "http://127.0.0.1:28765/api/v1/session/bootstrap?nonce="+nonce+"&next=/pool", nil))
	if rec.Code != 303 {
		t.Fatalf("状态码=%d，应为 303", rec.Code)
	}
	if got := rec.Header().Get("Location"); got != "/pool" {
		t.Fatalf("跳转目标=%q，应为 /pool", got)
	}
	cookies := rec.Result().Cookies()
	if len(cookies) != 1 || cookies[0].Name != access.SessionCookie || cookies[0].Value != gate.Token() {
		t.Fatalf("Cookie 未正确发放: %v", cookies)
	}
	if !cookies[0].HttpOnly || cookies[0].Secure {
		t.Fatalf("Cookie 属性不符: %+v", cookies[0])
	}
	// 票据单次有效
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "http://127.0.0.1:28765/api/v1/session/bootstrap?nonce="+nonce, nil))
	if rec.Code != 401 {
		t.Fatalf("票据复用状态码=%d，应为 401", rec.Code)
	}
}

func TestBootstrapOpenRedirectRejected(t *testing.T) {
	gate, h := sessionMux(t, false)
	nonce := CreateBootstrap()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "http://127.0.0.1:28765/api/v1/session/bootstrap?nonce="+nonce+"&next=//evil.com", nil))
	if rec.Code != 303 {
		t.Fatalf("状态码=%d", rec.Code)
	}
	if got := rec.Header().Get("Location"); got != "/" {
		t.Fatalf("站外跳转应回落首页，实际=%q", got)
	}
	_ = gate
}

func TestDevSessionGate(t *testing.T) {
	_, h := sessionMux(t, false)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "http://127.0.0.1:28765/api/v1/session/dev", nil))
	if rec.Code != 404 {
		t.Fatalf("关闭 dev 通道状态码=%d，应为 404", rec.Code)
	}

	gate, h := sessionMux(t, true)
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "http://127.0.0.1:28765/api/v1/session/dev", nil))
	if rec.Code != 200 {
		t.Fatalf("开启 dev 通道状态码=%d，应为 200", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), `"ok"`) {
		t.Fatalf("响应体不符: %s", rec.Body.String())
	}
	cookies := rec.Result().Cookies()
	if len(cookies) != 1 || cookies[0].Value != gate.Token() {
		t.Fatalf("Cookie 未发放: %v", cookies)
	}
}
