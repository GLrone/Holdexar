package systemx

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"holdexar/server-go/internal/access"
)

// unlockMux 装配解锁端点（固定密码，计数失败/成功回调）。
func unlockMux(t *testing.T, enabled bool) (*access.Gate, http.Handler, *int, *int) {
	t.Helper()
	gate := access.New()
	mux := http.NewServeMux()
	fails, successes := 0, 0
	RegisterSession(mux, gate, false, UnlockDeps{
		Enabled:  func() bool { return enabled },
		Password: func() (string, error) { return "lan-secret", nil },
		Fail: func() {
			fails++
			access.RecordUnlockFailure()
		},
		Success: func() {
			successes++
			access.RecordUnlockSuccess()
		},
	})
	t.Cleanup(access.ResetUnlockState)
	return gate, mux, &fails, &successes
}

func postUnlock(h http.Handler, body string) *httptest.ResponseRecorder {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "http://192.168.1.5:28765/api/v1/session/unlock", strings.NewReader(body))
	req.Host = "192.168.1.5:28765"
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

// 未开启局域网模式（或未接线解锁依赖）时端点恒 404——默认模式语义不变。
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

// 解锁全流程：错密码 401×5 → 第 6 次限速 429 → 限速清零后正确密码 200 发 Cookie。
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
	access.RecordUnlockSuccess() // 模拟限速窗口清零（真实实现为 30s 自然过期）
	rec := postUnlock(h, `{"password":" lan-secret "}`)
	if rec.Code != 200 {
		t.Fatalf("正确密码状态码=%d，应为 200", rec.Code)
	}
	if *successes != 1 {
		t.Fatalf("成功应记账 1 次，实际 %d", *successes)
	}
	cookies := rec.Result().Cookies()
	if len(cookies) != 1 || cookies[0].Name != access.SessionCookie || cookies[0].Value != gate.Token() {
		t.Fatalf("解锁应发放会话 Cookie: %v", cookies)
	}
	if !cookies[0].HttpOnly || cookies[0].Secure {
		t.Fatalf("Cookie 属性不符: %+v", cookies[0])
	}
}

// 请求体形状：非法 JSON 与缺/错型 password 均 422（Pydantic 同径）。
func TestUnlockBadBodyUnprocessable(t *testing.T) {
	_, h, _, _ := unlockMux(t, true)
	for _, body := range []string{`{bad`, ``, `{}`, `{"password":5}`} {
		if rec := postUnlock(h, body); rec.Code != 422 {
			t.Fatalf("body %q: 状态码=%d，应为 422", body, rec.Code)
		}
	}
}
