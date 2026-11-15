package settings

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"holdexar/server-go/internal/access"
	"holdexar/server-go/internal/secretbox"
)

// lanStateCleanup 局域网内存态隔离。
func lanStateCleanup(t *testing.T) {
	t.Cleanup(func() {
		access.SetLANEnabled(false)
		access.SetExtraHosts(nil)
	})
}

// lanGet 带 Host 头请求设置面。
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

// P0-1 回归：密码原文永不回显——LAN 模式下伪造任何 Host（127.0.0.1/
// localhost/[::1]/局域网地址）与伪造转发头（X-Forwarded-For/X-Real-IP）
// 都拿不到明文，读取面只有 has_password 布尔。
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
	// 伪造转发头同型：读取面不含任何明文
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

// PUT lan_access：enabled 落明 KV；掩码回显不回写；明文落密文 KV；
// 写后 Host 闸白名单按开关刷新。
func TestPutLanAccessPayload(t *testing.T) {
	lanStateCleanup(t)
	db, h := newTestEnv(t)

	// 掩码与空串都不落库；enabled 生效
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

	// 明文密码落密文 KV（闸外加密、闸内落库）
	rec = do(t, h, http.MethodPut, "/api/v1/settings", `{"lan_access":{"password":" new-secret "}}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("PUT 密码状态码 = %d, body %s", rec.Code, rec.Body.String())
	}
	var stored string
	if err := db.QueryRow("SELECT value_json FROM app_settings WHERE key = ?", keyLANPassword).Scan(&stored); err != nil {
		t.Fatalf("密码未落库：%v", err)
	}
	if !strings.HasPrefix(stored, `"enc1:`) {
		t.Fatalf("库内应为 enc1: 密文，实际 %s", stored)
	}
	token := strings.Trim(stored, `"`)
	plain, err := secretbox.DecryptSecretIn(resolveDataDir(), token, secretbox.SecretPurpose(keyLANPassword))
	if err != nil {
		t.Fatalf("密文解密失败：%v", err)
	}
	if plain != "new-secret" {
		t.Fatalf("还原明文不符：%q", plain)
	}
	if got := lanGet(t, h, "127.0.0.1:28765")["lan_access"].(map[string]any); got["has_password"] != true {
		t.Fatalf("写后状态位不符：%v", got)
	}

	// 关闭后白名单回落回环-only
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

// HostAllowedForTest 直查 access 闸白名单（测试镜像 HostAllowed 语义）。
func HostAllowedForTest(host string) bool {
	return access.HostAllowed(host)
}

// PUT lan_access 形状校验（Pydantic 同径：非对象/错型 422）。
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
