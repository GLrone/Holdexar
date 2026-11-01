package middleware

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// corsConfig 与现网装配一致（main.py add_middleware 的参数形状）。
func corsConfig() CORS {
	return CORS{
		AllowOrigins: []string{
			"http://localhost:5173", "http://127.0.0.1:5173",
			"http://localhost:8080", "http://127.0.0.1:8080",
		},
		AllowCredentials: true,
		AllowMethods:     []string{"*"},
		AllowHeaders:     []string{"*"},
	}
}

func corsHandler() http.Handler {
	return corsConfig().Middleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
}

func TestPreflightAllowedOrigin(t *testing.T) {
	req := httptest.NewRequest(http.MethodOptions, "/api/v1/games", nil)
	req.Header.Set("Origin", "http://localhost:5173")
	req.Header.Set("Access-Control-Request-Method", "GET")
	rec := httptest.NewRecorder()
	corsHandler().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK || rec.Body.String() != "OK" {
		t.Fatalf("预检应 200 OK: %d %q", rec.Code, rec.Body.String())
	}
	h := rec.Header()
	if got := h.Get("Access-Control-Allow-Origin"); got != "http://localhost:5173" {
		t.Fatalf("ACAO 应回显 Origin: %q", got)
	}
	if got := h.Get("Access-Control-Allow-Credentials"); got != "true" {
		t.Fatalf("ACAC: %q", got)
	}
	if got := h.Get("Access-Control-Allow-Methods"); got != "DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT, QUERY" {
		t.Fatalf("Allow-Methods: %q", got)
	}
	if got := h.Get("Access-Control-Max-Age"); got != "600" {
		t.Fatalf("Max-Age: %q", got)
	}
	if got := h.Get("Vary"); got != "Origin, Access-Control-Request-Method, Access-Control-Request-Headers, Access-Control-Request-Private-Network" {
		t.Fatalf("Vary: %q", got)
	}
	if got := h.Get("Content-Type"); got != "text/plain; charset=utf-8" {
		t.Fatalf("Content-Type: %q", got)
	}
}

// TestPreflightDisallowedOrigin 清单外 Origin 回 400，且放行方法/凭据头仍在、
// 仅缺 Access-Control-Allow-Origin（Starlette 失败响应保留基础头）。
func TestPreflightDisallowedOrigin(t *testing.T) {
	req := httptest.NewRequest(http.MethodOptions, "/api/v1/games", nil)
	req.Header.Set("Origin", "http://evil.example")
	req.Header.Set("Access-Control-Request-Method", "GET")
	rec := httptest.NewRecorder()
	corsHandler().ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest || rec.Body.String() != "Disallowed CORS origin" {
		t.Fatalf("应 400 Disallowed CORS origin: %d %q", rec.Code, rec.Body.String())
	}
	h := rec.Header()
	if h.Get("Access-Control-Allow-Origin") != "" {
		t.Fatalf("失败响应不得带 ACAO: %q", h.Get("Access-Control-Allow-Origin"))
	}
	if h.Get("Access-Control-Allow-Credentials") != "true" || h.Get("Access-Control-Max-Age") != "600" {
		t.Fatalf("失败响应应保留基础头: %v", h)
	}
}

func TestPreflightBadMethodKeepsACAO(t *testing.T) {
	req := httptest.NewRequest(http.MethodOptions, "/api/v1/games", nil)
	req.Header.Set("Origin", "http://localhost:5173")
	req.Header.Set("Access-Control-Request-Method", "TRACE")
	rec := httptest.NewRecorder()
	corsHandler().ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest || rec.Body.String() != "Disallowed CORS method" {
		t.Fatalf("应 400 Disallowed CORS method: %d %q", rec.Code, rec.Body.String())
	}
	if rec.Header().Get("Access-Control-Allow-Origin") != "http://localhost:5173" {
		t.Fatalf("Origin 合法时失败响应仍带 ACAO: %v", rec.Header())
	}
}

func TestPreflightMirrorsRequestHeaders(t *testing.T) {
	req := httptest.NewRequest(http.MethodOptions, "/api/v1/games", nil)
	req.Header.Set("Origin", "http://localhost:5173")
	req.Header.Set("Access-Control-Request-Method", "POST")
	req.Header.Set("Access-Control-Request-Headers", "X-Custom-A, Content-Type")
	rec := httptest.NewRecorder()
	corsHandler().ServeHTTP(rec, req)
	if got := rec.Header().Get("Access-Control-Allow-Headers"); got != "X-Custom-A, Content-Type" {
		t.Fatalf("ACAH 应镜像请求: %q", got)
	}
}

// TestPreflightExplicitHeaders 清单外请求头进失败集合；安全表头恒放行。
func TestPreflightExplicitHeaders(t *testing.T) {
	c := corsConfig()
	c.AllowHeaders = []string{"X-Allowed"}
	handler := c.Middleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	mk := func(hdr string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(http.MethodOptions, "/", nil)
		req.Header.Set("Origin", "http://localhost:5173")
		req.Header.Set("Access-Control-Request-Method", "POST")
		req.Header.Set("Access-Control-Request-Headers", hdr)
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		return rec
	}
	if rec := mk("X-Other"); rec.Code != 400 || rec.Body.String() != "Disallowed CORS headers" {
		t.Fatalf("清单外头应 400: %d %q", rec.Code, rec.Body.String())
	}
	if rec := mk("X-Allowed, Accept"); rec.Code != 200 {
		t.Fatalf("清单内头 + 安全表头应 200: %d", rec.Code)
	}
}

// TestSimpleResponses 简单响应放行矩阵：命中回显 Origin + 凭据 + Vary；
// 未命中只有凭据与 Vary；无 Origin 只有 Vary。
func TestSimpleResponses(t *testing.T) {
	cases := []struct {
		origin          string
		wantACAO        string
		wantACAC        string
		wantVaryHas     bool
		wantPassthrough bool
	}{
		{origin: "http://localhost:5173", wantACAO: "http://localhost:5173", wantACAC: "true", wantVaryHas: true},
		{origin: "http://evil.example", wantACAO: "", wantACAC: "true", wantVaryHas: true},
		// 空值 Origin 头仍是「在场」（Starlette 仅按头存在性判），凭据位照发。
		{origin: "", wantACAO: "", wantACAC: "true", wantVaryHas: true},
	}
	for _, tc := range cases {
		req := httptest.NewRequest(http.MethodGet, "/api/v1/games", nil)
		if tc.origin != "" {
			req.Header.Set("Origin", tc.origin)
		} else {
			req.Header["Origin"] = []string{""}
		}
		rec := httptest.NewRecorder()
		corsHandler().ServeHTTP(rec, req)
		h := rec.Header()
		if got := h.Get("Access-Control-Allow-Origin"); got != tc.wantACAO {
			t.Fatalf("Origin %q: ACAO 应 %q 得 %q", tc.origin, tc.wantACAO, got)
		}
		if got := h.Get("Access-Control-Allow-Credentials"); got != tc.wantACAC {
			t.Fatalf("Origin %q: ACAC 应 %q 得 %q", tc.origin, tc.wantACAC, got)
		}
		if tc.wantVaryHas && h.Get("Vary") != "Origin" {
			t.Fatalf("Origin %q: Vary 应为 Origin: %q", tc.origin, h.Get("Vary"))
		}
		if rec.Body.String() != `{"ok":true}` {
			t.Fatalf("内层响应应透传: %q", rec.Body.String())
		}
	}
}

// TestPreflightDoesNotEnterHandler 预检不进内层；无 ACRM 头的 OPTIONS 进内层。
func TestPreflightDoesNotEnterHandler(t *testing.T) {
	entered := false
	handler := corsConfig().Middleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		entered = true
	}))
	req := httptest.NewRequest(http.MethodOptions, "/api/v1/games", nil)
	req.Header.Set("Origin", "http://localhost:5173")
	req.Header.Set("Access-Control-Request-Method", "GET")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if entered {
		t.Fatal("预检不得进内层")
	}
	entered = false
	req = httptest.NewRequest(http.MethodOptions, "/api/v1/games", nil)
	req.Header.Set("Origin", "http://localhost:5173")
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if !entered {
		t.Fatal("无 ACRM 头的 OPTIONS 应进内层（路由 405 同径）")
	}
}

// TestSSEFlushPassthrough 流式响应经包装器后 Flush 仍可达且头已补写。
func TestSSEFlushPassthrough(t *testing.T) {
	handler := corsConfig().Middleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("data: x\n\n"))
		if f, ok := w.(http.Flusher); ok {
			f.Flush()
		} else {
			t.Error("包装器未透传 Flusher")
		}
	}))
	req := httptest.NewRequest(http.MethodGet, "/api/v1/events/stream", nil)
	req.Header.Set("Origin", "http://localhost:5173")
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Header().Get("Access-Control-Allow-Origin") != "http://localhost:5173" {
		t.Fatalf("流式响应缺 ACAO: %v", rec.Header())
	}
	if !strings.Contains(rec.Body.String(), "data: x") {
		t.Fatalf("流式体丢失: %q", rec.Body.String())
	}
}
