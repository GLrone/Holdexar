package access

import (
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
)

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

func TestSetLANEnabledFollowsKV(t *testing.T) {
	resetLAN(t)
	if !SetLANEnabled(true) || !LANEnabled() {
		t.Fatal("开启后应生效")
	}
	if SetLANEnabled(false) || LANEnabled() {
		t.Fatal("关闭后应失效")
	}
}

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

func TestMiddlewareLANHostPassAndRebindReject(t *testing.T) {
	resetLAN(t)
	gate := New()
	handler := gate.Middleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	SetLANEnabled(true)
	SetExtraHosts([]string{"192.168.1.5"})
	request := func(path string) *http.Request {
		req := httptest.NewRequest("GET", "http://192.168.1.5:28765"+path, nil)
		req.RemoteAddr = "192.168.1.25:51234"
		return req
	}
	req := request("/api/v1/health")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("本机地址 Host 应过闸，实际 %d", rec.Code)
	}
	attacker := request("/api/v1/health")
	attacker.Host = "rebound.attacker.example"
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, attacker)
	if rec.Code != http.StatusMisdirectedRequest {
		t.Fatalf("重绑定域名应 421，实际 %d", rec.Code)
	}
	private := request("/api/v1/monitor")
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, private)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("过闸后非公开路径应到会话闸 401，实际 %d", rec.Code)
	}
}

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

func TestGeneratePasswordShape(t *testing.T) {
	pw := GeneratePassword()
	if len(pw) != 12 {
		t.Fatalf("密码应 12 位，实际 %d", len(pw))
	}
	if strings.ContainsAny(pw, "+/=") {
		t.Fatalf("应 URL 安全字符：%s", pw)
	}
}

func TestEnvPasswordChannel(t *testing.T) {
	t.Setenv(EnvAccessPassword, " secret-lan-pw ")
	if EnvPassword() != "secret-lan-pw" {
		t.Fatalf("环境密码应 strip 后返回，实际 %q", EnvPassword())
	}
}

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

func TestPerIPUnlockBackoffAndPeerKey(t *testing.T) {
	resetLAN(t)
	cases := []struct {
		remote string
		want   string
	}{
		{"", "unknown"},
		{"127.0.0.1:51234", "loopback"},
		{"[::1]:51234", "loopback"},
		{"[::1]", "loopback"},
		{"[::ffff:127.0.0.1]:51234", "loopback"},
		{"192.0.2.1:1234", "192.0.2.1"},
		{"192.168.1.25:51234", "192.168.1.25"},
		{"[::ffff:192.168.1.25]:51234", "192.168.1.25"},
		{"[2001:db8::1]:51234", "2001:db8::1"},
	}
	for _, tc := range cases {
		if got := PeerKey(tc.remote); got != tc.want {
			t.Fatalf("PeerKey(%q)=%q, want %q", tc.remote, got, tc.want)
		}
	}

	attacker := PeerKey("192.168.1.25:51234")
	other := PeerKey("192.168.1.26:51234")
	loopback := PeerKey("127.0.0.1:51234")
	for range 5 {
		RecordUnlockFailureFor(attacker)
	}
	if UnlockLockedSeconds(attacker) <= 0 {
		t.Fatal("攻击对端应进入锁定")
	}
	if UnlockLockedSeconds(loopback) != 0 || UnlockLockedSeconds(other) != 0 {
		t.Fatalf("远端限速不应波及回环或其他设备: loopback=%v other=%v",
			UnlockLockedSeconds(loopback), UnlockLockedSeconds(other))
	}
	RecordUnlockSuccessFor(attacker)
	if UnlockLockedSeconds(attacker) != 0 {
		t.Fatal("RecordUnlockSuccessFor 应清零目标对端锁定")
	}
}
