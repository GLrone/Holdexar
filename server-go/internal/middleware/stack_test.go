package middleware

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// TestStackOverSocket 完整叠序（安全响应头 → CORS）在真实 socket 上的冒烟：
// 预检与简单响应的头都套上安全头（对齐 Python 最外层 _add_static_cache_headers）。
func TestStackOverSocket(t *testing.T) {
	srv := httptest.NewServer(Headers{}.Middleware(corsConfig().Middleware(
		http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			_, _ = w.Write([]byte("ok"))
		}))))
	defer srv.Close()

	req, _ := http.NewRequest(http.MethodOptions, srv.URL+"/api/v1/games", nil)
	req.Header.Set("Origin", "http://localhost:5173")
	req.Header.Set("Access-Control-Request-Method", "GET")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK || resp.Header.Get("Access-Control-Allow-Origin") != "http://localhost:5173" {
		t.Fatalf("预检不符: %d %v", resp.StatusCode, resp.Header)
	}
	if resp.Header.Get("X-Content-Type-Options") != "nosniff" || resp.Header.Get("X-Frame-Options") != "DENY" {
		t.Fatalf("预检响应缺安全头: %v", resp.Header)
	}

	resp2, err := http.Get(srv.URL + "/assets/app-aB3xY9zQ.js")
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	if got := resp2.Header.Get("Cache-Control"); got != "public, max-age=31536000, immutable" {
		t.Fatalf("指纹产物缓存头不符: %q", got)
	}
}
