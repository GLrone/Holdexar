package access

import (
	"crypto/tls"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
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
		{"127.0.0.1:evil", false},
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
	newLoopbackReq := func(method, target string) *http.Request {
		r := httptest.NewRequest(method, target, nil)
		r.RemoteAddr = "127.0.0.1:1234"
		return r
	}

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, newLoopbackReq("GET", "http://127.0.0.1:28765/page"))
	if rec.Code != 200 || rec.Header().Get("Content-Type") != "text/html; charset=utf-8" {
		t.Fatalf("锁定态页面导航=%d/%s，应为 200 text/html", rec.Code, rec.Header().Get("Content-Type"))
	}
	if !strings.Contains(rec.Body.String(), "holdexar-unlock-page") {
		t.Fatalf("锁定态应返回解锁页")
	}
	if strings.Contains(rec.Body.String(), ">page<") {
		t.Fatalf("解锁页混入了受保护内容")
	}
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, newLoopbackReq("GET", "http://127.0.0.1:28765/assets/app-abc12345.js"))
	if rec.Code != 404 {
		t.Fatalf("锁定态资源探测=%d，应为 404", rec.Code)
	}
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, newLoopbackReq("GET", "http://127.0.0.1:28765/favicon.ico"))
	if rec.Code != 404 {
		t.Fatalf("锁定态图标探测=%d，应为 404", rec.Code)
	}
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, newLoopbackReq("POST", "http://127.0.0.1:28765/page"))
	if rec.Code != 404 {
		t.Fatalf("锁定态写操作=%d，应为 404", rec.Code)
	}
	rec = httptest.NewRecorder()
	req := newLoopbackReq("GET", "http://127.0.0.1:28765/page")
	req.AddCookie(&http.Cookie{Name: SessionCookie, Value: g.Token()})
	h.ServeHTTP(rec, req)
	if rec.Code != 200 || rec.Body.String() != "page" {
		t.Fatalf("会话放行状态码/体=%d/%s", rec.Code, rec.Body.String())
	}
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, newLoopbackReq("GET", "http://127.0.0.1:28765/api/v1/health"))
	if rec.Code != 200 {
		t.Fatalf("健康检查被拦截: %d", rec.Code)
	}
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, newLoopbackReq("GET", "http://127.0.0.1:28765/api/v1/regions"))
	if rec.Code != 401 {
		t.Fatalf("无会话状态码=%d，应为 401", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "缺少有效的本地会话") {
		t.Fatalf("401 文案不符: %s", rec.Body.String())
	}
	rec = httptest.NewRecorder()
	req = newLoopbackReq("GET", "http://127.0.0.1:28765/api/v1/regions")
	req.AddCookie(&http.Cookie{Name: SessionCookie, Value: "forged"})
	h.ServeHTTP(rec, req)
	if rec.Code != 401 {
		t.Fatalf("伪造令牌状态码=%d，应为 401", rec.Code)
	}
	rec = httptest.NewRecorder()
	req = newLoopbackReq("GET", "http://127.0.0.1:28765/api/v1/regions")
	req.AddCookie(&http.Cookie{Name: SessionCookie, Value: g.Token()})
	h.ServeHTTP(rec, req)
	if rec.Code != 200 {
		t.Fatalf("Cookie 会话状态码=%d，应为 200", rec.Code)
	}
	rec = httptest.NewRecorder()
	req = newLoopbackReq("GET", "http://127.0.0.1:28765/api/v1/regions")
	req.Header.Set(SessionHeader, g.Token())
	h.ServeHTTP(rec, req)
	if rec.Code != 200 {
		t.Fatalf("请求头会话状态码=%d，应为 200", rec.Code)
	}
	rec = httptest.NewRecorder()
	req = newLoopbackReq("GET", "http://127.0.0.1:28765/api/v1/health")
	req.Host = "evil.com"
	h.ServeHTTP(rec, req)
	if rec.Code != 421 {
		t.Fatalf("坏 Host 状态码=%d，应为 421", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "请求来源不被信任") {
		t.Fatalf("421 文案不符: %s", rec.Body.String())
	}
	rec = httptest.NewRecorder()
	req = newLoopbackReq("GET", "http://127.0.0.1:28765/api/v1/regions")
	req.Header.Set("Origin", "http://localhost:5173")
	h.ServeHTTP(rec, req)
	if rec.Code != 403 {
		t.Fatalf("跨端口 Origin 状态码=%d，应为 403", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "请求来源与应用不同源") {
		t.Fatalf("403 文案不符: %s", rec.Body.String())
	}
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, newLoopbackReq("OPTIONS", "http://127.0.0.1:28765/api/v1/regions"))
	if rec.Code == 401 {
		t.Fatalf("OPTIONS 被会话闸拦截")
	}
	if g.Token() != g.Token() {
		t.Fatalf("令牌应进程内稳定")
	}
	if len(g.Token()) != 43 {
		t.Fatalf("令牌长度=%d，应为 43（32 字节 urlsafe 无填充）", len(g.Token()))
	}
}

func issueCookieForTest(t *testing.T, g *Gate) *http.Cookie {
	t.Helper()
	rec := httptest.NewRecorder()
	g.Issue(rec)
	cookies := rec.Result().Cookies()
	if len(cookies) != 1 || cookies[0].Name != SessionCookie {
		t.Fatalf("Issue 未下发会话 Cookie: %+v", cookies)
	}
	return cookies[0]
}

func TestMultiSessionIsolationAndRevoke(t *testing.T) {
	g := New()
	c1 := issueCookieForTest(t, g)
	c2 := issueCookieForTest(t, g)
	if c1.Value == c2.Value {
		t.Fatalf("两次登录不应共享同一会话令牌: %q", c1.Value)
	}
	if len(c1.Value) != 43 || len(c2.Value) != 43 {
		t.Fatalf("会话令牌长度异常: %d/%d", len(c1.Value), len(c2.Value))
	}
	if !c1.HttpOnly || c1.SameSite != http.SameSiteStrictMode || c1.Path != "/" {
		t.Fatalf("Cookie 属性不符: %+v", c1)
	}

	req1 := httptest.NewRequest("GET", "/api/v1/x", nil)
	req1.AddCookie(c1)
	req2 := httptest.NewRequest("GET", "/api/v1/x", nil)
	req2.AddCookie(c2)
	if !g.Authorized(req1) || !g.Authorized(req2) {
		t.Fatal("两个已签发会话都应通过鉴权")
	}

	compat := g.Token()
	if compat == c1.Value || compat == c2.Value {
		t.Fatal("兼容 Token() 不应与登录态 Cookie 混淆")
	}
	rec := httptest.NewRecorder()
	g.RevokeSession(rec, req1)
	cleared := rec.Result().Cookies()
	if len(cleared) != 1 || cleared[0].Name != SessionCookie || cleared[0].MaxAge >= 0 || cleared[0].Value != "" {
		t.Fatalf("吊销未下发过期 Cookie: %+v", cleared)
	}
	if !cleared[0].HttpOnly || cleared[0].SameSite != http.SameSiteStrictMode || cleared[0].Path != "/" {
		t.Fatalf("吊销 Cookie 属性不符: %+v", cleared[0])
	}
	if g.Authorized(req1) {
		t.Fatal("被吊销的会话 1 仍被放行")
	}
	if !g.Authorized(req2) {
		t.Fatal("未吊销的会话 2 被误伤")
	}

	hdrReq := httptest.NewRequest("POST", "/api/v1/session/logout", nil)
	hdrReq.Header.Set(SessionHeader, compat)
	g.RevokeSession(httptest.NewRecorder(), hdrReq)
	if g.Authorized(hdrReq) {
		t.Fatal("请求头携带的令牌吊销后仍被放行")
	}
	if g.Token() == compat {
		t.Fatal("兼容令牌吊销后未轮换")
	}
}

func TestResetAndInvalidateSessions(t *testing.T) {
	g := New()
	c1 := issueCookieForTest(t, g)
	c2 := issueCookieForTest(t, g)
	compat := g.Token()

	g.InvalidateSessions()

	for _, tok := range []string{c1.Value, c2.Value, compat} {
		req := httptest.NewRequest("GET", "/api/v1/x", nil)
		req.Header.Set(SessionHeader, tok)
		if g.Authorized(req) {
			t.Fatalf("InvalidateSessions 后旧令牌仍有效: %q", tok)
		}
	}
	if g.Token() == compat {
		t.Fatal("Reset 后 Token() 应重新生成")
	}
}

func TestSessionTTLExpiryAndPruning(t *testing.T) {
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	g := New()
	g.now = func() time.Time { return now }

	c1 := issueCookieForTest(t, g)
	compat := g.Token()

	now = now.Add(sessionTTL - time.Second)
	req1 := httptest.NewRequest("GET", "/api/v1/x", nil)
	req1.AddCookie(c1)
	if !g.Authorized(req1) {
		t.Fatal("12 小时到期前会话应仍有效")
	}

	now = now.Add(2 * time.Second)
	if g.Authorized(req1) {
		t.Fatal("超过 12 小时 TTL 后会话应失效")
	}
	reqCompat := httptest.NewRequest("GET", "/api/v1/x", nil)
	reqCompat.Header.Set(SessionHeader, compat)
	if g.Authorized(reqCompat) {
		t.Fatal("超过 12 小时 TTL 后兼容令牌应失效")
	}

	c2 := issueCookieForTest(t, g)
	g.mu.Lock()
	count := len(g.sessions)
	_, hasOld := g.sessions[c1.Value]
	_, hasNew := g.sessions[c2.Value]
	g.mu.Unlock()
	if count != 1 || hasOld || !hasNew {
		t.Fatalf("过期会话未清扫: count=%d hasOld=%v hasNew=%v", count, hasOld, hasNew)
	}
}

func TestPeerIsLoopbackVariants(t *testing.T) {
	cases := []struct {
		addr string
		want bool
	}{
		{"", false},
		{"127.0.0.1:51234", true},
		{"[::1]:51234", true},
		{"[::1]", true},
		{"[::ffff:127.0.0.1]:51234", true},
		{"192.0.2.1:1234", false},
		{"192.168.1.25:51234", false},
		{"[2001:db8::1]:51234", false},
		{"not-an-ip", false},
	}
	for _, tc := range cases {
		if got := PeerIsLoopback(tc.addr); got != tc.want {
			t.Errorf("PeerIsLoopback(%q)=%v, want %v", tc.addr, got, tc.want)
		}
	}
}
