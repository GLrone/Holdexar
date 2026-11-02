package middleware

import (
	"compress/gzip"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func gzipHandler(body string, ct string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", ct)
		_, _ = w.Write([]byte(body))
	})
}

func doReq(h http.Handler, ae string) *httptest.ResponseRecorder {
	req := httptest.NewRequest("GET", "/x", nil)
	if ae != "" {
		req.Header.Set("Accept-Encoding", ae)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	return w
}

func TestGzipCompressesLargeResponse(t *testing.T) {
	body := strings.Repeat(" Holdexar 压缩测试", 100) // >500 bytes
	h := GZip{}.Middleware(gzipHandler(body, "application/json"))
	w := doReq(h, "gzip, deflate")
	if w.Code != 200 {
		t.Fatalf("状态 %d", w.Code)
	}
	if w.Header().Get("Content-Encoding") != "gzip" {
		t.Fatalf("缺 Content-Encoding: %v", w.Header())
	}
	if !strings.Contains(w.Header().Get("Vary"), "Accept-Encoding") {
		t.Fatalf("缺 Vary: %v", w.Header())
	}
	zr, err := gzip.NewReader(w.Body)
	if err != nil {
		t.Fatalf("gzip 头无效: %v", err)
	}
	got, _ := io.ReadAll(zr)
	if string(got) != body {
		t.Fatal("解压后与原体不等")
	}
}

func TestGzipSmallResponsePassesThrough(t *testing.T) {
	h := GZip{}.Middleware(gzipHandler("tiny", "application/json"))
	w := doReq(h, "gzip")
	if w.Header().Get("Content-Encoding") != "" {
		t.Fatal("小响应不应压缩")
	}
	if w.Body.String() != "tiny" {
		t.Fatalf("体不符: %q", w.Body.String())
	}
	if strings.Contains(w.Header().Get("Vary"), "Accept-Encoding") {
		t.Fatal("阈值下不加 Vary")
	}
}

func TestGzipSSEExempt(t *testing.T) {
	h := GZip{}.Middleware(gzipHandler(strings.Repeat("data: x\n\n", 200), "text/event-stream; charset=utf-8"))
	w := doReq(h, "gzip")
	if w.Header().Get("Content-Encoding") != "" {
		t.Fatal("SSE 不应压缩")
	}
	if !strings.HasPrefix(w.Body.String(), "data: x") {
		t.Fatal("SSE 体应原样透传")
	}
}

func TestGzipIdentityAddsVary(t *testing.T) {
	body := strings.Repeat("x", 1000)
	h := GZip{}.Middleware(gzipHandler(body, "application/json"))
	w := doReq(h, "deflate") // 无 gzip
	if w.Header().Get("Content-Encoding") != "" {
		t.Fatal("identity 不应压缩")
	}
	if !strings.Contains(w.Header().Get("Vary"), "Accept-Encoding") {
		t.Fatal("identity ≥500 应加 Vary")
	}
	if w.Body.String() != body {
		t.Fatal("identity 体不符")
	}
}

func TestGzipExistingEncodingSkipped(t *testing.T) {
	h := GZip{}.Middleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Encoding", "br")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(strings.Repeat("x", 1000)))
	}))
	w := doReq(h, "gzip")
	if w.Header().Get("Content-Encoding") != "br" {
		t.Fatal("已有 Content-Encoding 应保留")
	}
}

func TestGzipBufferedBelowThresholdOnReturn(t *testing.T) {
	// 多次小 Write 累计仍 <500：handler 返回后透传
	h := GZip{}.Middleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte("ab"))
		_, _ = w.Write([]byte("cd"))
	}))
	w := doReq(h, "gzip")
	if w.Body.String() != "abcd" || w.Header().Get("Content-Encoding") != "" {
		t.Fatalf("缓冲透传不符: %q %v", w.Body.String(), w.Header())
	}
}

func TestGzipBufferedCrossingThresholdCompresses(t *testing.T) {
	// 多次小 Write 累计跨过 500：压缩启用
	h := GZip{}.Middleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		for i := 0; i < 60; i++ {
			_, _ = w.Write([]byte("0123456789"))
		}
	}))
	w := doReq(h, "gzip")
	if w.Header().Get("Content-Encoding") != "gzip" {
		t.Fatal("跨阈值应压缩")
	}
	zr, _ := gzip.NewReader(w.Body)
	got, _ := io.ReadAll(zr)
	if len(got) != 600 {
		t.Fatalf("解压长度 %d", len(got))
	}
}
