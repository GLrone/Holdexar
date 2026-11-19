package settings

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"holdexar/server-go/internal/access"
	"holdexar/server-go/internal/events"
	"holdexar/server-go/internal/secretbox"
	"holdexar/server-go/internal/store"
)

func testHex(t *testing.T, n int) string {
	t.Helper()
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		t.Fatal(err)
	}
	return hex.EncodeToString(b)
}

func lanStateCleanup(t *testing.T) {
	t.Cleanup(func() {
		access.SetLANEnabled(false)
		access.SetExtraHosts(nil)
	})
}

func lanGet(t *testing.T, h http.Handler, host string) map[string]any {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/settings", nil)
	req.Host = host
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("GET 状态码 = %d, body %s", rec.Code, rec.Body.String())
	}
	var resp map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("响应解析失败：%v", err)
	}
	return resp
}

func TestLanAccessPasswordNeverEchoed(t *testing.T) {
	lanStateCleanup(t)
	_, h := newTestEnv(t)
	access.SetLANEnabled(true)
	t.Setenv(access.EnvAccessPassword, "env-lan-pw")

	for _, host := range []string{"127.0.0.1:28765", "localhost:28765", "[::1]:28765", "192.168.1.5:28765"} {
		lan := lanGet(t, h, host)["lan_access"].(map[string]any)
		if _, present := lan["password"]; present {
			t.Fatalf("Host %s: 响应不得含 password 字段（P0-1 回归）", host)
		}
		if lan["has_password"] != true || lan["enabled"] != true {
			t.Fatalf("Host %s: 状态位不符：%v", host, lan)
		}
	}
	for _, hdr := range []string{"X-Forwarded-For", "X-Real-IP"} {
		req := httptest.NewRequest(http.MethodGet, "/api/v1/settings", nil)
		req.Host = "192.168.1.5:28765"
		req.Header.Set(hdr, "127.0.0.1")
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusOK {
			t.Fatalf("伪造 %s: 状态码 = %d", hdr, rec.Code)
		}
		var resp map[string]any
		if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
			t.Fatalf("响应解析失败：%v", err)
		}
		lan := resp["lan_access"].(map[string]any)
		if _, present := lan["password"]; present {
			t.Fatalf("伪造 %s: 响应不得含 password 字段（P0-1 回归）", hdr)
		}
		if lan["has_password"] != true {
			t.Fatalf("伪造 %s: has_password 不符：%v", hdr, lan)
		}
	}
}

func TestPutLanAccessPayload(t *testing.T) {
	lanStateCleanup(t)
	db, h := newTestEnv(t)

	rec := do(t, h, http.MethodPut, "/api/v1/settings", `{"lan_access":{"enabled":true,"password":"****abcd"}}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("PUT 状态码 = %d, body %s", rec.Code, rec.Body.String())
	}
	var n int
	if err := db.QueryRow("SELECT COUNT(*) FROM app_settings WHERE key = ?", keyLANPassword).Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != 0 {
		t.Fatalf("掩码不得回写，实际 %d 行", n)
	}
	if !access.LANEnabled() {
		t.Fatal("enabled 写入应即时生效（内存态）")
	}

	short := fmt.Sprintf(`{"lan_access":{"password":%q}}`, testHex(t, 2))
	rec = do(t, h, http.MethodPut, "/api/v1/settings", short)
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("短密码状态码 = %d, want 422, body %s", rec.Code, rec.Body.String())
	}

	pw := "pw-" + testHex(t, 6)
	rec = do(t, h, http.MethodPut, "/api/v1/settings", fmt.Sprintf(`{"lan_access":{"password":" %s "}}`, pw))
	if rec.Code != http.StatusOK {
		t.Fatalf("PUT 密码状态码 = %d, body %s", rec.Code, rec.Body.String())
	}
	var stored string
	if err := db.QueryRow("SELECT value_json FROM app_settings WHERE key = ?", KeyLANPasswordHash).Scan(&stored); err != nil {
		t.Fatalf("验证器未落库：%v", err)
	}
	phc := strings.Trim(stored, `"`)
	if !strings.HasPrefix(phc, "$scrypt$") || !access.VerifyPassword(pw, phc) {
		t.Fatalf("库内应为 $scrypt$ 验证器且校验通过：%s", phc)
	}
	n = 0
	if err := db.QueryRow("SELECT COUNT(*) FROM app_settings WHERE key = ?", keyLANPassword).Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != 0 {
		t.Fatalf("旧密文键应清账，实际 %d 行", n)
	}
	if got := lanGet(t, h, "127.0.0.1:28765")["lan_access"].(map[string]any); got["has_password"] != true {
		t.Fatalf("写后状态位不符：%v", got)
	}

	rec = do(t, h, http.MethodPut, "/api/v1/settings", `{"lan_access":{"enabled":false}}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("PUT 关闭状态码 = %d", rec.Code)
	}
	if access.LANEnabled() {
		t.Fatal("关闭应即时生效")
	}
	if HostAllowedForTest("192.168.1.5") {
		t.Fatal("关闭后白名单应回落回环-only")
	}
}

func HostAllowedForTest(host string) bool {
	return access.HostAllowed(host)
}

func TestPutLanPasswordRotatesSessions(t *testing.T) {
	lanStateCleanup(t)
	dir := t.TempDir()
	path := filepath.Join(dir, "holdexar.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatalf("打开临时库失败：%v", err)
	}
	t.Cleanup(func() { db.Close() })
	if _, err := db.Exec(`CREATE TABLE app_settings (
    key VARCHAR(64) NOT NULL PRIMARY KEY,
    value_json TEXT,
    updated_at DATETIME
)`); err != nil {
		t.Fatalf("建表失败：%v", err)
	}
	prev := resolveDataDir
	resolveDataDir = func() string { return dir }
	t.Cleanup(func() { resolveDataDir = prev })
	sessionGate := access.New()
	mux := http.NewServeMux()
	Register(mux, db, store.NewWriteGate(db, path), Options{SessionGate: sessionGate})

	pw1 := "pw-" + testHex(t, 6)
	pw2 := "pw-" + testHex(t, 6)
	if rec := do(t, mux, http.MethodPut, "/api/v1/settings", fmt.Sprintf(`{"lan_access":{"password":%q}}`, pw1)); rec.Code != http.StatusOK {
		t.Fatalf("首次设密码状态码 = %d, body %s", rec.Code, rec.Body.String())
	}
	oldToken := sessionGate.Token()
	if rec := do(t, mux, http.MethodPut, "/api/v1/settings", fmt.Sprintf(`{"lan_access":{"password":%q}}`, pw2)); rec.Code != http.StatusOK {
		t.Fatalf("改密状态码 = %d, body %s", rec.Code, rec.Body.String())
	}
	if sessionGate.Token() == oldToken {
		t.Fatal("改密应轮换会话令牌（旧解锁会话全失效）")
	}
}

func TestPutLanAccessValidationShapes(t *testing.T) {
	lanStateCleanup(t)
	_, h := newTestEnv(t)
	for _, body := range []string{`{"lan_access":"nope"}`, `{"lan_access":{"enabled":"maybe"}}`, `{"lan_access":{"password":5}}`} {
		rec := do(t, h, http.MethodPut, "/api/v1/settings", body)
		if rec.Code != http.StatusUnprocessableEntity {
			t.Fatalf("body %s: 状态码 = %d, want 422", body, rec.Code)
		}
	}
}

func TestLanPasswordSourcePrecedence(t *testing.T) {
	lanStateCleanup(t)
	db, h := newTestEnv(t)

	lan := lanGet(t, h, "127.0.0.1:28765")["lan_access"].(map[string]any)
	if lan["password_source"] != "none" || lan["has_password"] != false {
		t.Fatalf("无密码时状态不符: %v", lan)
	}

	t.Setenv(access.EnvAccessPassword, "env-pw-12345678")
	lan = lanGet(t, h, "127.0.0.1:28765")["lan_access"].(map[string]any)
	if lan["password_source"] != "env" || lan["has_password"] != true {
		t.Fatalf("环境口令时状态不符: %v", lan)
	}

	sealed, err := secretbox.EncryptSecretIn(resolveDataDir(), "legacy-user-pw", secretbox.SecretPurpose(keyLANPassword))
	if err != nil {
		t.Fatal(err)
	}
	seedKV(t, db, keyLANPassword, fmt.Sprintf("%q", sealed))
	lan = lanGet(t, h, "127.0.0.1:28765")["lan_access"].(map[string]any)
	if lan["password_source"] != "user" || lan["has_password"] != true {
		t.Fatalf("旧密文存在时应为 user: %v", lan)
	}

	if _, err := db.Exec("DELETE FROM app_settings WHERE key = ?", keyLANPassword); err != nil {
		t.Fatal(err)
	}
	seedKV(t, db, KeyLANPasswordHash, fmt.Sprintf("%q", access.HashPassword("user-pw-12345678")))
	lan = lanGet(t, h, "127.0.0.1:28765")["lan_access"].(map[string]any)
	if lan["password_source"] != "user" || lan["has_password"] != true {
		t.Fatalf("PHC 哈希存在时应为 user: %v", lan)
	}
}

func TestPutLanEmptyPasswordClearsAndRotatesAndNotifies(t *testing.T) {
	lanStateCleanup(t)
	dir := t.TempDir()
	path := filepath.Join(dir, "holdexar.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if _, err := db.Exec(`CREATE TABLE app_settings (
    key VARCHAR(64) NOT NULL PRIMARY KEY,
    value_json TEXT,
    updated_at DATETIME
)`); err != nil {
		t.Fatal(err)
	}
	prev := resolveDataDir
	resolveDataDir = func() string { return dir }
	t.Cleanup(func() { resolveDataDir = prev })

	bus := events.NewBus()
	sub := bus.Subscribe()
	defer bus.Unsubscribe(sub)
	sessionGate := access.New()
	mux := http.NewServeMux()
	Register(mux, db, store.NewWriteGate(db, path), Options{SessionGate: sessionGate, Bus: bus})

	pw := "pw-" + testHex(t, 6)
	if rec := do(t, mux, http.MethodPut, "/api/v1/settings", fmt.Sprintf(`{"lan_access":{"password":%q}}`, pw)); rec.Code != http.StatusOK {
		t.Fatalf("设密码状态码=%d body=%s", rec.Code, rec.Body.String())
	}
	drain := func() events.Event {
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		ev, err := sub.Get(ctx)
		if err != nil {
			t.Fatalf("未收到 settings.changed 事件: %v", err)
		}
		return ev
	}
	_ = drain()

	seedKV(t, db, keyLANPassword, `"legacy-cipher"`)
	w := httptest.NewRecorder()
	sessionGate.Issue(w)
	oldCookie := w.Result().Cookies()[0]

	rec := do(t, mux, http.MethodPut, "/api/v1/settings", `{"lan_access":{"password":""}}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("清空密码状态码=%d body=%s", rec.Code, rec.Body.String())
	}
	var count int
	if err := db.QueryRow("SELECT COUNT(*) FROM app_settings WHERE key IN (?, ?)", KeyLANPasswordHash, keyLANPassword).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 0 {
		t.Fatalf("清空密码后双键应全部删除，剩余 %d 行", count)
	}

	ev := drain()
	if ev.Type != "settings.changed" {
		t.Fatalf("事件类型=%s，应为 settings.changed", ev.Type)
	}

	oldReq := httptest.NewRequest(http.MethodGet, "/api/v1/x", nil)
	oldReq.AddCookie(oldCookie)
	if sessionGate.Authorized(oldReq) {
		t.Fatal("清空密码后旧会话未失效")
	}
	newCookies := rec.Result().Cookies()
	if len(newCookies) != 1 || newCookies[0].Value == oldCookie.Value {
		t.Fatalf("清空密码后未重签会话 Cookie: %+v", newCookies)
	}
	newReq := httptest.NewRequest(http.MethodGet, "/api/v1/x", nil)
	newReq.AddCookie(newCookies[0])
	if !sessionGate.Authorized(newReq) {
		t.Fatal("清空密码后重签 Cookie 无效")
	}
}

func TestPutLanEnvToUserRotatesSessions(t *testing.T) {
	lanStateCleanup(t)
	dir := t.TempDir()
	path := filepath.Join(dir, "holdexar.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if _, err := db.Exec(`CREATE TABLE app_settings (
    key VARCHAR(64) NOT NULL PRIMARY KEY,
    value_json TEXT,
    updated_at DATETIME
)`); err != nil {
		t.Fatal(err)
	}
	prev := resolveDataDir
	resolveDataDir = func() string { return dir }
	t.Cleanup(func() { resolveDataDir = prev })

	t.Setenv(access.EnvAccessPassword, "env-pw-12345678")
	sessionGate := access.New()
	mux := http.NewServeMux()
	Register(mux, db, store.NewWriteGate(db, path), Options{SessionGate: sessionGate})

	w := httptest.NewRecorder()
	sessionGate.Issue(w)
	envSession := w.Result().Cookies()[0]

	rec := do(t, mux, http.MethodPut, "/api/v1/settings", fmt.Sprintf(`{"lan_access":{"password":%q}}`, "user-"+testHex(t, 6)))
	if rec.Code != http.StatusOK {
		t.Fatalf("env→user 改密状态码=%d body=%s", rec.Code, rec.Body.String())
	}
	oldReq := httptest.NewRequest(http.MethodGet, "/api/v1/x", nil)
	oldReq.AddCookie(envSession)
	if sessionGate.Authorized(oldReq) {
		t.Fatal("env→user 改密后旧会话未吊销")
	}
	newCookies := rec.Result().Cookies()
	if len(newCookies) != 1 || newCookies[0].Value == envSession.Value {
		t.Fatalf("env→user 改密后未重签新会话: %+v", newCookies)
	}
}
