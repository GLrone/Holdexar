package systemx

import (
	"context"
	"database/sql"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"holdexar/server-go/internal/access"
	"holdexar/server-go/internal/secretbox"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
)

func sessionMux(t *testing.T, dev bool) (*access.Gate, http.Handler) {
	t.Helper()
	gate := access.New()
	mux := http.NewServeMux()
	RegisterSession(mux, gate, dev, UnlockDeps{})
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

// unlockEnv 解锁通道测试环境：真实临时库 + 写闸 + 隔离数据目录。
func unlockEnv(t *testing.T) (*sql.DB, UnlockDeps, http.Handler, *access.Gate) {
	t.Helper()
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
	gate := store.NewWriteGate(db, path)
	deps := UnlockDeps{DB: db, WriteGate: gate, DataDir: dir}
	accessGate := access.New()
	mux := http.NewServeMux()
	RegisterSession(mux, accessGate, false, deps)
	access.ResetLANState()
	t.Cleanup(access.ResetLANState)
	return db, deps, mux, accessGate
}

func seedJSON(t *testing.T, db *sql.DB, key, jsonText string) {
	t.Helper()
	if _, err := db.Exec(
		"INSERT INTO app_settings (key, value_json, updated_at) VALUES (?, ?, NULL) "+
			"ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
		key, jsonText); err != nil {
		t.Fatalf("种子写入失败：%v", err)
	}
}

func postUnlock(t *testing.T, h http.Handler, password string) *httptest.ResponseRecorder {
	t.Helper()
	rec := httptest.NewRecorder()
	req := httptest.NewRequest("POST", "http://192.168.1.5:28765/api/v1/session/unlock",
		strings.NewReader(`{"password":`+strconv.Quote(password)+`}`))
	req.Host = "192.168.1.5:28765"
	h.ServeHTTP(rec, req)
	return rec
}

// enableLANWithPassword 开启局域网模式并注入固定密码（DB 无关路径）。
func enableLANWithPassword(t *testing.T, db *sql.DB, password string) {
	t.Helper()
	access.SetEnabled(true)
	access.SetExtraHosts(access.LocalIPs())
	seedJSON(t, db, settings.LANPasswordKey, strconv.Quote(password))
}

func TestUnlockDisabled404(t *testing.T) {
	_, _, h, _ := unlockEnv(t)
	rec := postUnlock(t, h, "whatever")
	if rec.Code != 404 {
		t.Fatalf("默认模式解锁入口状态码=%d，应为 404", rec.Code)
	}
}

func TestUnlockFlowWrongPasswordThenLimit(t *testing.T) {
	db, _, h, gate := unlockEnv(t)
	enableLANWithPassword(t, db, "pw-test-1234")
	for i := 0; i < 5; i++ {
		if rec := postUnlock(t, h, "wrong-"+strconv.Itoa(i)); rec.Code != 401 {
			t.Fatalf("第 %d 次错密码状态码=%d，应为 401", i+1, rec.Code)
		}
	}
	// 连续失败达阈值后全局锁定：正确密码也 429。
	if rec := postUnlock(t, h, "pw-test-1234"); rec.Code != 429 {
		t.Fatalf("限速状态码=%d，应为 429", rec.Code)
	}
	access.ResetUnlockState()
	rec := postUnlock(t, h, "pw-test-1234")
	if rec.Code != 200 {
		t.Fatalf("正确密码状态码=%d，应为 200", rec.Code)
	}
	cookies := rec.Result().Cookies()
	if len(cookies) != 1 || cookies[0].Name != access.SessionCookie || cookies[0].Value != gate.Token() {
		t.Fatalf("解锁 Cookie 未正确发放: %v", cookies)
	}
}

func TestUnlockWrongPasswordBody401(t *testing.T) {
	db, _, h, _ := unlockEnv(t)
	enableLANWithPassword(t, db, "pw-test-1234")
	if rec := postUnlock(t, h, "totally-wrong"); rec.Code != 401 {
		t.Fatalf("错密码状态码=%d，应为 401", rec.Code)
	}
	if !strings.Contains(postUnlock(t, h, "totally-wrong").Body.String(), "访问密码不正确") {
		t.Fatalf("401 文案不符")
	}
}

func TestUnlockEnvPasswordChannel(t *testing.T) {
	db, _, h, _ := unlockEnv(t)
	access.SetEnabled(true)
	access.SetExtraHosts(access.LocalIPs())
	t.Setenv(access.EnvAccessPassword, "env-pw-12345")
	if rec := postUnlock(t, h, "env-pw-12345"); rec.Code != 200 {
		t.Fatalf("环境变量密码解锁状态码=%d，应为 200", rec.Code)
	}
	_ = db
}

func TestUnlockMissingPassword422(t *testing.T) {
	db, _, h, _ := unlockEnv(t)
	enableLANWithPassword(t, db, "pw-test-1234")
	rec := httptest.NewRecorder()
	req := httptest.NewRequest("POST", "http://127.0.0.1:28765/api/v1/session/unlock", strings.NewReader(`{}`))
	h.ServeHTTP(rec, req)
	if rec.Code != 422 {
		t.Fatalf("缺 password 字段状态码=%d，应为 422", rec.Code)
	}
}

func TestEnsureLANPasswordGeneratesOnce(t *testing.T) {
	db, deps, _, _ := unlockEnv(t)
	ctx := context.Background()
	first, err := EnsureLANPassword(ctx, deps)
	if err != nil {
		t.Fatalf("首次生成失败：%v", err)
	}
	second, err := EnsureLANPassword(ctx, deps)
	if err != nil {
		t.Fatalf("回读失败：%v", err)
	}
	if first == "" || first != second {
		t.Fatalf("密码应生成一次并回读同值：%q vs %q", first, second)
	}
	// 库内是 enc1: 密文，且可解回明文。
	var stored string
	if err := db.QueryRow(
		"SELECT value_json FROM app_settings WHERE key = ?", settings.LANPasswordKey).Scan(&stored); err != nil {
		t.Fatalf("读回失败：%v", err)
	}
	token := strings.Trim(stored, `"`)
	if !strings.HasPrefix(token, secretbox.Prefix) {
		t.Fatalf("库内应为 enc1: 密文，实际 %s", token)
	}
	plain, err := secretbox.DecryptSecretIn(deps.DataDir, token, secretbox.SecretPurpose(settings.LANPasswordKey))
	if err != nil || plain != first {
		t.Fatalf("密文解密不符：%v %q", err, plain)
	}
}

func TestInitLANAccessToggle(t *testing.T) {
	db, deps, _, _ := unlockEnv(t)
	ctx := context.Background()
	// 默认关闭：开关落地为关闭并回退回环-only。
	if enabled, err := InitLANAccess(ctx, deps); err != nil || enabled {
		t.Fatalf("无开关默认初始化应为关闭：%v %v", enabled, err)
	}
	if access.HostAllowed("192.168.1.5:28765") {
		t.Fatalf("关闭态不应放行局域网地址")
	}
	// 开关开启：生效并放行本机地址集，密码已生成落库。
	seedJSON(t, db, settings.LANEnabledKey, `true`)
	enabled, err := InitLANAccess(ctx, deps)
	if err != nil || !enabled {
		t.Fatalf("开启初始化失败：%v %v", enabled, err)
	}
	if !access.IsEnabled() {
		t.Fatalf("初始化后应生效")
	}
	for _, ip := range access.LocalIPs() {
		if !access.HostAllowed("["+ip+"]:28765") {
			t.Fatalf("本机地址 %s 应入白名单", ip)
		}
	}
	var password string
	if err := db.QueryRow(
		"SELECT value_json FROM app_settings WHERE key = ?", settings.LANPasswordKey).Scan(&password); err != nil || strings.Trim(password, `"`) == "" {
		t.Fatalf("开启时应已生成访问密码：%v", err)
	}
	// 异常开关值 fail-closed。
	seedJSON(t, db, settings.LANEnabledKey, `not-json`)
	access.ResetLANState()
	if _, err := InitLANAccess(ctx, deps); err == nil {
		t.Fatalf("异常开关值应返回错误")
	}
	if access.IsEnabled() {
		t.Fatalf("初始化失败应按关闭处理")
	}
}
