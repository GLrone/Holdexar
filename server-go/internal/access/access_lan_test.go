package access

import (
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
)

// resetLAN 局域网状态隔离：每个用例收尾恢复默认回环-only 与零限速。
func resetLAN(t *testing.T) {
	t.Helper()
	t.Cleanup(func() {
		SetExtraHosts(nil)
		SetLANEnabled(false)
		ResetUnlockState()
		_ = os.Unsetenv(EnvAccess)
		_ = os.Unsetenv(EnvAccessPassword)
	})
}

// 环境变量强制开启优先于设置 KV 通道。
func TestEnvForcedLAN(t *testing.T) {
	resetLAN(t)
	if EnvForced() || LANEnabled() {
		t.Fatal("未配置环境变量不应强制开启")
	}
	t.Setenv(EnvAccess, "lan")
	if !EnvForced() {
		t.Fatal("HOLDEXAR_ACCESS=lan 应强制开启")
	}
	SetLANEnabled(false)
	if !LANEnabled() {
		t.Fatal("环境变量强制开启优先于 KV 缓存开关")
	}
	t.Setenv(EnvAccess, "off")
	if EnvForced() || LANEnabled() {
		t.Fatal("非 lan 值不开启")
	}
}

// KV 通道开关随设置读写（无环境变量时生效状态即缓存值）。
func TestSetLANEnabledFollowsKV(t *testing.T) {
	resetLAN(t)
	if !SetLANEnabled(true) || !LANEnabled() {
		t.Fatal("开启后应生效")
	}
	if SetLANEnabled(false) || LANEnabled() {
		t.Fatal("关闭后应失效")
	}
}

// Host 白名单：本机地址放行、重绑定域名拒绝、关闭回落回环-only。
func TestExtraHostsGate(t *testing.T) {
	resetLAN(t)
	SetExtraHosts([]string{"192.168.1.5:28765", "192.168.1.5", "[fe80::1]:28765", "LOCALHOST"})
	for _, host := range []string{"192.168.1.5:28765", "192.168.1.5", "[fe80::1]:28765", "localhost:9999"} {
		if !HostAllowed(host) {
			t.Fatalf("HostAllowed(%q) 应放行", host)
		}
	}
	for _, host := range []string{"evil.com", "192.168.1.5.evil.com", ""} {
		if HostAllowed(host) {
			t.Fatalf("HostAllowed(%q) 应拒绝", host)
		}
	}
	if !LoopbackHost("[::1]:28765") || LoopbackHost("192.168.1.5:28765") {
		t.Fatal("回环判定不符")
	}
	SetExtraHosts(nil)
	if HostAllowed("192.168.1.5:28765") {
		t.Fatal("关闭后白名单应回到回环-only")
	}
	if !HostAllowed("127.0.0.1:28765") {
		t.Fatal("回环默认放行不得受影响")
	}
}

// 中间件：局域网模式放行本机地址 Host（公开路径直达、非公开路径到会话闸）；
// 重绑定域名仍 421。
func TestMiddlewareLANHostPassAndRebindReject(t *testing.T) {
	resetLAN(t)
	gate := New()
	handler := gate.Middleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	SetExtraHosts([]string{"192.168.1.5"})
	req := httptest.NewRequest("GET", "http://192.168.1.5:28765/api/v1/health", nil)
	req.Host = "192.168.1.5:28765"
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("本机地址 Host 应过闸，实际 %d", rec.Code)
	}
	attacker := httptest.NewRequest("GET", "http://192.168.1.5:28765/api/v1/health", nil)
	attacker.Host = "rebound.attacker.example"
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, attacker)
	if rec.Code != http.StatusMisdirectedRequest {
		t.Fatalf("重绑定域名应 421，实际 %d", rec.Code)
	}
	private := httptest.NewRequest("GET", "http://192.168.1.5:28765/api/v1/monitor", nil)
	private.Host = "192.168.1.5:28765"
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, private)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("过闸后非公开路径应到会话闸 401，实际 %d", rec.Code)
	}
}

// Origin 闸复用 Host 白名单：局域网同源请求通过。
func TestOriginAllowedLANOrigin(t *testing.T) {
	resetLAN(t)
	SetExtraHosts([]string{"192.168.1.5"})
	r := httptest.NewRequest("GET", "http://192.168.1.5:28765/api/v1/x", nil)
	r.Host = "192.168.1.5:28765"
	r.Header.Set("Origin", "http://192.168.1.5:28765")
	if !OriginAllowed(r) {
		t.Fatal("局域网同源请求应通过 Origin 闸")
	}
}

// 解锁限速：连续失败达阈值锁定，成功清零。
func TestUnlockRateLimit(t *testing.T) {
	resetLAN(t)
	if UnlockLockedSeconds() != 0 {
		t.Fatal("初始不限速")
	}
	for range 5 {
		RecordUnlockFailure()
	}
	if UnlockLockedSeconds() <= 0 {
		t.Fatal("连续失败 5 次应进入锁定窗口")
	}
	RecordUnlockSuccess()
	if UnlockLockedSeconds() != 0 {
		t.Fatal("成功应清零限速")
	}
}

// 访问密码形态：12 位 URL 安全字符。
func TestGeneratePasswordShape(t *testing.T) {
	pw := GeneratePassword()
	if len(pw) != 12 {
		t.Fatalf("密码应 12 位，实际 %d", len(pw))
	}
	if strings.ContainsAny(pw, "+/=") {
		t.Fatalf("应 URL 安全字符：%s", pw)
	}
}

// 环境密码通道（值 strip）。
func TestEnvPasswordChannel(t *testing.T) {
	t.Setenv(EnvAccessPassword, " secret-lan-pw ")
	if EnvPassword() != "secret-lan-pw" {
		t.Fatalf("环境密码应 strip 后返回，实际 %q", EnvPassword())
	}
}

// 本机地址集至少含回环，且不含链路本地段。
func TestLocalIPsIncludesLoopback(t *testing.T) {
	ips := LocalIPs()
	has := func(want string) bool {
		for _, ip := range ips {
			if ip == want {
				return true
			}
		}
		return false
	}
	if !has("127.0.0.1") || !has("localhost") {
		t.Fatalf("本机地址集应含回环，实际 %v", ips)
	}
	for _, ip := range ips {
		if strings.HasPrefix(ip, "169.254.") {
			t.Fatalf("链路本地地址应被过滤：%v", ips)
		}
	}
}
