package access

import (
	"crypto/tls"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestHostAllowed(t *testing.T) {
	cases := []struct {
		host string
		want bool
	}{
		{"127.0.0.1:28765", true},
		{"localhost", true},
		{"localhost:8080", true},
		{"LOCALHOST:8080", true},
		{"[::1]:2880", true},
		{"[::1]", true},
		{"[::2]:2880", false},
		{"evil.com", false},
		{"127.0.0.1.evil.com", false},
		{"127.0.0.2", false},
		{"", false},
	}
	for _, c := range cases {
		if got := HostAllowed(c.host); got != c.want {
			t.Errorf("HostAllowed(%q)=%v，应为 %v", c.host, got, c.want)
		}
	}
}

func testRequest(host, origin string) *http.Request {
	r := httptest.NewRequest("GET", "http://127.0.0.1:28765/api/v1/x", nil)
	r.Host = host
	if origin != "" {
		r.Header.Set("Origin", origin)
	}
	return r
}

func TestOriginAllowed(t *testing.T) {
	cases := []struct {
		name   string
		host   string
		origin string
		tls    bool
		want   bool
	}{
		{"无 Origin 放行", "127.0.0.1:28765", "", false, true},
		{"null 拒绝", "127.0.0.1:28765", "null", false, false},
		{"同源同端口", "127.0.0.1:28765", "http://127.0.0.1:28765", false, true},
		{"同主机跨端口拒绝", "127.0.0.1:28765", "http://localhost:5173", false, false},
		{"IPv6 同源", "[::1]:28765", "http://[::1]:28765", false, true},
		{"攻击者域名拒绝", "127.0.0.1:28765", "http://evil.com:28765", false, false},
		{"非 http scheme 拒绝", "127.0.0.1:28765", "ftp://127.0.0.1:28765", false, false},
		{"https 同源按 443 缺省端口", "127.0.0.1", "https://127.0.0.1", true, true},
		{"https 缺省端口不等", "127.0.0.1:28765", "https://127.0.0.1", true, false},
	}
	for _, c := range cases {
		r := testRequest(c.host, c.origin)
		if c.tls {
			r.TLS = &tls.ConnectionState{}
		}
		if got := OriginAllowed(r); got != c.want {
			t.Errorf("%s: OriginAllowed=%v，应为 %v", c.name, got, c.want)
		}
	}
}

func newGateHandler(t *testing.T) (*Gate, http.Handler) {
	t.Helper()
	g := New()
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/v1/health", func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("ok"))
	})
	mux.HandleFunc("GET /api/v1/regions", func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("regions"))
	})
	mux.HandleFunc("GET /api/v1/secret", func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("secret"))
	})
	mux.HandleFunc("GET /page", func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("page"))
	})
	return g, g.Middleware(mux)
}

func TestMiddlewareGate(t *testing.T) {
	g, h := newGateHandler(t)

	// 非 API 路径不拦
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "http://127.0.0.1:28765/page", nil))
	if rec.Code != 200 {
		t.Fatalf("静态路径被拦截: %d", rec.Code)
	}
	// 健康检查公开
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "http://127.0.0.1:28765/api/v1/health", nil))
	if rec.Code != 200 {
		t.Fatalf("健康检查被拦截: %d", rec.Code)
	}
	// 无会话 401
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "http://127.0.0.1:28765/api/v1/regions", nil))
	if rec.Code != 401 {
		t.Fatalf("无会话状态码=%d，应为 401", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "缺少有效的本地会话") {
		t.Fatalf("401 文案不符: %s", rec.Body.String())
	}
	// 伪造令牌 401
	rec = httptest.NewRecorder()
	req := httptest.NewRequest("GET", "http://127.0.0.1:28765/api/v1/regions", nil)
	req.AddCookie(&http.Cookie{Name: SessionCookie, Value: "forged"})
	h.ServeHTTP(rec, req)
	if rec.Code != 401 {
		t.Fatalf("伪造令牌状态码=%d，应为 401", rec.Code)
	}
	// Cookie 正确令牌放行
	rec = httptest.NewRecorder()
	req = httptest.NewRequest("GET", "http://127.0.0.1:28765/api/v1/regions", nil)
	req.AddCookie(&http.Cookie{Name: SessionCookie, Value: g.Token()})
	h.ServeHTTP(rec, req)
	if rec.Code != 200 {
		t.Fatalf("Cookie 会话状态码=%d，应为 200", rec.Code)
	}
	// 请求头会话放行
	rec = httptest.NewRecorder()
	req = httptest.NewRequest("GET", "http://127.0.0.1:28765/api/v1/regions", nil)
	req.Header.Set(SessionHeader, g.Token())
	h.ServeHTTP(rec, req)
	if rec.Code != 200 {
		t.Fatalf("请求头会话状态码=%d，应为 200", rec.Code)
	}
	// 坏 Host 421
	rec = httptest.NewRecorder()
	req = httptest.NewRequest("GET", "http://127.0.0.1:28765/api/v1/health", nil)
	req.Host = "evil.com"
	h.ServeHTTP(rec, req)
	if rec.Code != 421 {
		t.Fatalf("坏 Host 状态码=%d，应为 421", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "请求来源不被信任") {
		t.Fatalf("421 文案不符: %s", rec.Body.String())
	}
	// 跨端口 Origin 403（无会话也应先过 Origin 闸）
	rec = httptest.NewRecorder()
	req = httptest.NewRequest("GET", "http://127.0.0.1:28765/api/v1/regions", nil)
	req.Header.Set("Origin", "http://localhost:5173")
	h.ServeHTTP(rec, req)
	if rec.Code != 403 {
		t.Fatalf("跨端口 Origin 状态码=%d，应为 403", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "请求来源与应用不同源") {
		t.Fatalf("403 文案不符: %s", rec.Body.String())
	}
	// OPTIONS 穿过会话闸
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("OPTIONS", "http://127.0.0.1:28765/api/v1/regions", nil))
	if rec.Code == 401 {
		t.Fatalf("OPTIONS 被会话闸拦截")
	}
	// 令牌稳定且不外泄
	if g.Token() != g.Token() {
		t.Fatalf("令牌应进程内稳定")
	}
	if len(g.Token()) != 43 {
		t.Fatalf("令牌长度=%d，应为 43（32 字节 urlsafe 无填充）", len(g.Token()))
	}
}
