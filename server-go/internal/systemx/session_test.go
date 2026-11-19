package systemx

import (
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"holdexar/server-go/internal/access"
)

func unlockMux(t *testing.T, enabled bool) (*access.Gate, http.Handler, *int, *int) {
	t.Helper()
	access.ResetUnlockState()
	gate := access.New()
	mux := http.NewServeMux()
	fails, successes := 0, 0
	RegisterSession(mux, gate, false, UnlockDeps{
		Enabled:  func() bool { return enabled },
		Verifier: func() (string, error) { return access.HashPassword("lan-secret"), nil },
		FailPeer: func(peer string) {
			fails++
			access.RecordUnlockFailureFor(peer)
		},
		SuccessPeer: func(peer string) {
			successes++
			access.RecordUnlockSuccessFor(peer)
		},
	})
	t.Cleanup(access.ResetUnlockState)
	return gate, mux, &fails, &successes
}

func postUnlock(h http.Handler, body string) *httptest.ResponseRecorder {
	return postUnlockFrom(h, body, "192.168.1.25")
}

func postUnlockFrom(h http.Handler, body, peerIP string) *httptest.ResponseRecorder {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "http://192.168.1.5:28765/api/v1/session/unlock", strings.NewReader(body))
	req.Host = "192.168.1.5:28765"
	req.RemoteAddr = net.JoinHostPort(peerIP, "51234")
	h.ServeHTTP(rec, req)
	return rec
}

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
	if len(cookies) != 1 || cookies[0].Name != access.SessionCookie || len(cookies[0].Value) != 43 {
		t.Fatalf("Cookie 未正确发放: %+v", cookies)
	}
	authReq := httptest.NewRequest("GET", "http://127.0.0.1:28765/api/v1/x", nil)
	authReq.AddCookie(cookies[0])
	if !gate.Authorized(authReq) {
		t.Fatalf("发放的引导 Cookie 未被 Gate 接纳")
	}
	if !cookies[0].HttpOnly || cookies[0].Secure {
		t.Fatalf("Cookie 属性不符: %+v", cookies[0])
	}
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
	local := httptest.NewRequest("GET", "http://127.0.0.1:28765/api/v1/session/dev", nil)
	local.RemoteAddr = "127.0.0.1:51234"
	h.ServeHTTP(rec, local)
	if rec.Code != 200 {
		t.Fatalf("开启 dev 通道状态码=%d，应为 200", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), `"ok"`) {
		t.Fatalf("响应体不符: %s", rec.Body.String())
	}
	cookies := rec.Result().Cookies()
	if len(cookies) != 1 || cookies[0].Name != access.SessionCookie || len(cookies[0].Value) != 43 {
		t.Fatalf("Cookie 未发放: %+v", cookies)
	}
	authReq := httptest.NewRequest("GET", "http://127.0.0.1:28765/api/v1/x", nil)
	authReq.AddCookie(cookies[0])
	if !gate.Authorized(authReq) {
		t.Fatalf("发放的 dev Cookie 未被 Gate 接纳")
	}

	rec = httptest.NewRecorder()
	remote := httptest.NewRequest("GET", "http://127.0.0.1:28765/api/v1/session/dev", nil)
	remote.RemoteAddr = "192.168.1.25:51234"
	h.ServeHTTP(rec, remote)
	if rec.Code != 404 || len(rec.Result().Cookies()) != 0 {
		t.Fatalf("局域网对端伪造回环 Host 不得取得 dev 会话：status=%d cookies=%v", rec.Code, rec.Result().Cookies())
	}
}

func TestUnlockDisabledNotFound(t *testing.T) {
	_, h, fails, successes := unlockMux(t, false)
	if rec := postUnlock(h, `{"password":"lan-secret"}`); rec.Code != 404 {
		t.Fatalf("关闭状态码=%d，应为 404", rec.Code)
	}
	if *fails != 0 || *successes != 0 {
		t.Fatal("关闭态不得触碰限速计数")
	}
	mux := http.NewServeMux()
	RegisterSession(mux, access.New(), false)
	if rec := postUnlock(mux, `{"password":"lan-secret"}`); rec.Code != 404 {
		t.Fatalf("未接线解锁依赖状态码=%d，应为 404", rec.Code)
	}
}

func TestUnlockWithoutPasswordRejected(t *testing.T) {
	gate := access.New()
	mux := http.NewServeMux()
	fails := 0
	RegisterSession(mux, gate, false, UnlockDeps{
		Enabled:  func() bool { return true },
		Verifier: func() (string, error) { return "", nil },
		FailPeer: func(peer string) {
			fails++
			access.RecordUnlockFailureFor(peer)
		},
		SuccessPeer: func(peer string) { access.RecordUnlockSuccessFor(peer) },
	})
	t.Cleanup(access.ResetUnlockState)
	if rec := postUnlock(mux, `{"password":"unused-test-value"}`); rec.Code != 401 {
		t.Fatalf("未设密码状态码=%d，应为 401", rec.Code)
	}
	if fails != 1 {
		t.Fatalf("未设密码应计入限速，实际 %d", fails)
	}
}

func TestUnlockFlowWrongThenLimitThenSuccess(t *testing.T) {
	gate, h, fails, successes := unlockMux(t, true)
	for range 5 {
		if rec := postUnlock(h, `{"password":"wrong"}`); rec.Code != 401 {
			t.Fatalf("错密码状态码=%d，应为 401", rec.Code)
		}
	}
	if *fails != 5 {
		t.Fatalf("失败应记账 5 次，实际 %d", *fails)
	}
	if rec := postUnlock(h, `{"password":"lan-secret"}`); rec.Code != 429 {
		t.Fatalf("连续失败后应限速 429，实际 %d", rec.Code)
	}
	access.RecordUnlockSuccess("192.168.1.25")
	rec := postUnlock(h, `{"password":" lan-secret "}`)
	if rec.Code != 200 {
		t.Fatalf("正确密码状态码=%d，应为 200", rec.Code)
	}
	if *successes != 1 {
		t.Fatalf("成功应记账 1 次，实际 %d", *successes)
	}
	cookies := rec.Result().Cookies()
	if len(cookies) != 1 || cookies[0].Name != access.SessionCookie || len(cookies[0].Value) != 43 {
		t.Fatalf("解锁应发放会话 Cookie: %+v", cookies)
	}
	authReq := httptest.NewRequest("GET", "http://127.0.0.1:28765/api/v1/x", nil)
	authReq.AddCookie(cookies[0])
	if !gate.Authorized(authReq) {
		t.Fatalf("解锁 Cookie 未被 Gate 接纳")
	}
	if !cookies[0].HttpOnly || cookies[0].Secure {
		t.Fatalf("Cookie 属性不符: %+v", cookies[0])
	}
}

func TestUnlockRateLimitIsolatedByRemotePeer(t *testing.T) {
	_, h, _, _ := unlockMux(t, true)
	for range 5 {
		if rec := postUnlockFrom(h, `{"password":"wrong"}`, "192.168.1.25"); rec.Code != http.StatusUnauthorized {
			t.Fatalf("攻击对端错密码状态码=%d，应为 401", rec.Code)
		}
	}
	if rec := postUnlockFrom(h, `{"password":"lan-secret"}`, "192.168.1.25"); rec.Code != http.StatusTooManyRequests {
		t.Fatalf("攻击对端达阈值后状态码=%d，应为 429", rec.Code)
	}
	if rec := postUnlockFrom(h, `{"password":"lan-secret"}`, "192.168.1.26"); rec.Code != http.StatusOK {
		t.Fatalf("另一局域网对端应仍可解锁，状态码=%d，body=%s", rec.Code, rec.Body.String())
	}
}

func TestUnlockBadBodyUnprocessable(t *testing.T) {
	_, h, _, _ := unlockMux(t, true)
	for _, body := range []string{`{bad`, ``, `{}`, `{"password":5}`} {
		if rec := postUnlock(h, body); rec.Code != 422 {
			t.Fatalf("body %q: 状态码=%d，应为 422", body, rec.Code)
		}
	}
}

func TestLogoutRevokesOnlyCallerSession(t *testing.T) {
	gate := access.New()
	mux := http.NewServeMux()
	RegisterSession(mux, gate, true)
	mux.HandleFunc("GET /api/v1/protected", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
	})
	h := gate.Middleware(mux)

	login := func() *http.Cookie {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "http://127.0.0.1:28765/api/v1/session/dev", nil)
		req.RemoteAddr = "127.0.0.1:51234"
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusOK {
			t.Fatalf("dev 登录状态码=%d", rec.Code)
		}
		return rec.Result().Cookies()[0]
	}
	c1 := login()
	c2 := login()
	if c1.Value == c2.Value {
		t.Fatalf("两次登录 Cookie 不应相同: %q", c1.Value)
	}

	anon := httptest.NewRecorder()
	anonReq := httptest.NewRequest(http.MethodPost, "http://127.0.0.1:28765/api/v1/session/logout", nil)
	anonReq.RemoteAddr = "127.0.0.1:1234"
	h.ServeHTTP(anon, anonReq)
	if anon.Code != http.StatusUnauthorized {
		t.Fatalf("无会话 logout 状态码=%d，应为 401", anon.Code)
	}

	outRec := httptest.NewRecorder()
	outReq := httptest.NewRequest(http.MethodPost, "http://127.0.0.1:28765/api/v1/session/logout", nil)
	outReq.RemoteAddr = "127.0.0.1:1234"
	outReq.AddCookie(c1)
	h.ServeHTTP(outRec, outReq)
	if outRec.Code != http.StatusNoContent {
		t.Fatalf("logout 状态码=%d，应为 204", outRec.Code)
	}
	if cookies := outRec.Result().Cookies(); len(cookies) != 1 || cookies[0].MaxAge >= 0 || cookies[0].Value != "" {
		t.Fatalf("logout 未返回过期 Cookie: %+v", cookies)
	}

	for name, tc := range map[string]struct {
		cookie *http.Cookie
		want   int
	}{
		"revoked": {c1, http.StatusUnauthorized},
		"other":   {c2, http.StatusOK},
	} {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "http://127.0.0.1:28765/api/v1/protected", nil)
		req.RemoteAddr = "127.0.0.1:1234"
		req.AddCookie(tc.cookie)
		h.ServeHTTP(rec, req)
		if rec.Code != tc.want {
			t.Fatalf("%s 访问受保护端点状态码=%d，应为 %d", name, rec.Code, tc.want)
		}
	}
}

func TestBootstrapNextRejectsBackslashAndControlChars(t *testing.T) {
	_, h := sessionMux(t, false)
	for _, next := range []string{`/\evil.example`, "/\t/evil.example", "/\x00/evil", "/\x7f/evil", "//evil.com", "https://evil.com"} {
		nonce := CreateBootstrap()
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "http://127.0.0.1:28765/api/v1/session/bootstrap", nil)
		q := req.URL.Query()
		q.Set("nonce", nonce)
		q.Set("next", next)
		req.URL.RawQuery = q.Encode()
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusSeeOther {
			t.Fatalf("next=%q 状态码=%d", next, rec.Code)
		}
		if got := rec.Header().Get("Location"); got != "/" {
			t.Fatalf("危险 next=%q 应回落首页，实际=%q", next, got)
		}
	}
	nonce := CreateBootstrap()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "http://127.0.0.1:28765/api/v1/session/bootstrap?nonce="+nonce+"&next=/dashboard", nil))
	if got := rec.Header().Get("Location"); got != "/dashboard" {
		t.Fatalf("合法站内跳转=%q，应为 /dashboard", got)
	}
}

func TestUnlockNoDoubleCountAndLoopbackIsolatedFromLegacyDeps(t *testing.T) {
	access.ResetUnlockState()
	t.Cleanup(access.ResetUnlockState)
	gate := access.New()
	mux := http.NewServeMux()
	RegisterSession(mux, gate, false, UnlockDeps{
		Enabled:  func() bool { return true },
		Verifier: func() (string, error) { return access.HashPassword("lan-secret"), nil },
		Fail:     func() { access.RecordUnlockFailure() },
		Success:  func() { access.RecordUnlockSuccess() },
	})

	for range 5 {
		if rec := postUnlockFrom(mux, `{"password":"wrong"}`, "192.168.1.25"); rec.Code != http.StatusUnauthorized {
			t.Fatalf("远端错密码状态码=%d", rec.Code)
		}
	}
	if access.UnlockLockedSeconds("loopback") != 0 {
		t.Fatalf("远端解锁失败污染了 loopback: %v", access.UnlockLockedSeconds("loopback"))
	}

	for i := range 5 {
		rec := postUnlockFrom(mux, `{"password":"wrong"}`, "127.0.0.1")
		if rec.Code != http.StatusUnauthorized {
			t.Fatalf("loopback 第 %d 次失败被提前锁定: %d", i+1, rec.Code)
		}
	}
	if rec := postUnlockFrom(mux, `{"password":"lan-secret"}`, "127.0.0.1"); rec.Code != http.StatusTooManyRequests {
		t.Fatalf("loopback 第 5 次后应进入 429，实际 %d", rec.Code)
	}
}
