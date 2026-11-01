package middleware

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func headersHandler() http.Handler {
	h := Headers{WriteProtected: func() bool { return false }}
	next := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	})
	return h.Middleware(next)
}

func TestSecurityHeadersOnAllResponses(t *testing.T) {
	cases := []string{
		"/api/v1/games",
		"/api/v1/nope",
		"/",
		"/assets/app-aB3xY9zQ.js",
	}
	for _, path := range cases {
		req := httptest.NewRequest(http.MethodGet, path, nil)
		rec := httptest.NewRecorder()
		headersHandler().ServeHTTP(rec, req)
		h := rec.Header()
		if h.Get("X-Content-Type-Options") != "nosniff" ||
			h.Get("X-Frame-Options") != "DENY" ||
			h.Get("Referrer-Policy") != "no-referrer" {
			t.Fatalf("%s 安全头缺失: %v", path, h)
		}
		want := "default-src 'self'; img-src 'self' data: https://cdn.cloudflare.steamstatic.com " +
			"https://shared.akamai.steamstatic.com https://store.fastly.steamstatic.com; " +
			"style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; " +
			"frame-ancestors 'none'; object-src 'none'; base-uri 'self'"
		if h.Get("Content-Security-Policy") != want {
			t.Fatalf("%s CSP 不符: %q", path, h.Get("Content-Security-Policy"))
		}
	}
}

// TestCacheControlTiers 缓存分档：API 不干预、指纹 immutable、无指纹一天、
// 其余 no-cache（与 Python 实例实测锚点逐值一致）。
func TestCacheControlTiers(t *testing.T) {
	cases := []struct {
		path string
		want string
	}{
		{"/api/v1/games", ""},
		{"/api/v1/health", ""},
		{"/docs", ""},
		{"/openapi.json", ""},
		{"/redoc", ""},
		{"/assets/app-aB3xY9zQ.js", "public, max-age=31536000, immutable"},
		{"/assets/index-B1c2D3e4.css", "public, max-age=31536000, immutable"},
		{"/logo.png", "public, max-age=86400"},
		{"/assets/fonts/rubik.woff2", "public, max-age=86400"},
		{"/assets/app-abcdefg.js", "public, max-age=86400"}, // 指纹段非 8 位
		{"/", "no-cache"},
		{"/index.html", "no-cache"},
		{"/games/some-route", "no-cache"},
	}
	for _, tc := range cases {
		if got := cacheControlFor(tc.path); got != tc.want {
			t.Fatalf("cacheControlFor(%q) 应 %q 得 %q", tc.path, tc.want, got)
		}
	}
}

// TestInnerCacheControlWins 内层已设 Cache-Control 时外层不覆盖（setdefault）。
func TestInnerCacheControlWins(t *testing.T) {
	next := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		_, _ = w.Write([]byte("x"))
	})
	req := httptest.NewRequest(http.MethodGet, "/assets/app-aB3xY9zQ.js", nil)
	rec := httptest.NewRecorder()
	Headers{}.Middleware(next).ServeHTTP(rec, req)
	if got := rec.Header().Get("Cache-Control"); got != "no-store" {
		t.Fatalf("内层 Cache-Control 应保留: %q", got)
	}
}

// TestUpdateWriteProtection 更新验收期：受保护时非只读 API 回 503，
// GET/HEAD/OPTIONS 与 update-ready 端点放行，非 API 路径不拦。
func TestUpdateWriteProtection(t *testing.T) {
	next := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("hit"))
	})
	handler := Headers{WriteProtected: func() bool { return true }}.Middleware(next)

	req := httptest.NewRequest(http.MethodPost, "/api/v1/settings", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("受保护 POST 应 503: %d", rec.Code)
	}
	var body struct {
		Detail string `json:"detail"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil || body.Detail != "正在完成更新，请稍后再试" {
		t.Fatalf("503 文案不符: %q %v", rec.Body.String(), err)
	}
	if rec.Header().Get("X-Content-Type-Options") != "nosniff" {
		t.Fatalf("503 也应带安全头: %v", rec.Header())
	}

	for _, tc := range []struct{ method, path string }{
		{http.MethodGet, "/api/v1/games"},
		{http.MethodHead, "/api/v1/games"},
		{http.MethodOptions, "/api/v1/games"},
		{http.MethodPost, "/api/v1/system/update-ready"},
		{http.MethodPost, "/page"},
	} {
		req := httptest.NewRequest(tc.method, tc.path, nil)
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		if rec.Code != http.StatusOK || rec.Body.String() != "hit" {
			t.Fatalf("%s %s 应放行: %d %q", tc.method, tc.path, rec.Code, rec.Body.String())
		}
	}

	unprotected := Headers{}.Middleware(next)
	req = httptest.NewRequest(http.MethodPost, "/api/v1/settings", nil)
	rec = httptest.NewRecorder()
	unprotected.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("无保护位 POST 应放行: %d", rec.Code)
	}
}
