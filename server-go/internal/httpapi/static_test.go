package httpapi

import (
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeDist(t *testing.T) string {
	t.Helper()
	dist := filepath.Join(t.TempDir(), "dist")
	if err := os.MkdirAll(filepath.Join(dist, "assets"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dist, "index.html"), []byte("<html>index-ok</html>"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dist, "assets", "app.abc.js"), []byte("console.log('asset-ok');"), 0o644); err != nil {
		t.Fatal(err)
	}
	return dist
}

func TestServeAssetFile(t *testing.T) {
	srv := &Server{DataDir: t.TempDir(), DistDir: writeDist(t)}
	rec := do(t, srv, "GET", "/assets/app.abc.js")
	if rec.Code != 200 {
		t.Fatalf("静态资源状态码=%d，应为 200", rec.Code)
	}
	if body := rec.Body.String(); !strings.Contains(body, "asset-ok") {
		t.Fatalf("静态资源内容不符：%s", body)
	}
	if ct := rec.Header().Get("Content-Type"); !strings.Contains(ct, "javascript") {
		t.Fatalf("Content-Type=%s，应为 javascript", ct)
	}
}

func TestMissingAssetReturns404JSON(t *testing.T) {
	srv := &Server{DataDir: t.TempDir(), DistDir: writeDist(t)}
	rec := do(t, srv, "GET", "/assets/missing.js")
	if rec.Code != 404 {
		t.Fatalf("缺失资源状态码=%d，应为 404", rec.Code)
	}
	assertDetailNull(t, decodeJSON(t, rec.Body.Bytes()))
}

func TestIndexFallback(t *testing.T) {
	srv := &Server{DataDir: t.TempDir(), DistDir: writeDist(t)}
	for _, target := range []string{"/", "/settings/prices", "/favicon.ico"} {
		rec := do(t, srv, "GET", target)
		if rec.Code != 200 {
			t.Fatalf("%s 回退状态码=%d，应为 200", target, rec.Code)
		}
		if body := rec.Body.String(); !strings.Contains(body, "index-ok") {
			t.Fatalf("%s 未回退到 index.html：%s", target, body)
		}
	}
}

func TestTraversalFallsBackToIndex(t *testing.T) {
	srv := &Server{DataDir: t.TempDir(), DistDir: writeDist(t)}
	for _, target := range []string{"/../secret.txt", "/assets/../../../go.mod", `/..\..\secret.txt`} {
		rec := httptest.NewRecorder()
		srv.handleStatic(rec, httptest.NewRequest("GET", target, nil))
		if rec.Code != 200 {
			t.Fatalf("%s 穿越请求状态码=%d，应回 200", target, rec.Code)
		}
		if body := rec.Body.String(); !strings.Contains(body, "index-ok") {
			t.Fatalf("%s 穿越请求未回退到 index.html：%s", target, body)
		}
		if strings.Contains(rec.Body.String(), "asset-ok") {
			t.Fatalf("%s 穿越请求读到了构建产物之外的内容", target)
		}
	}
}

func TestDistMissingGraceful404(t *testing.T) {
	srv := &Server{DataDir: t.TempDir(), DistDir: filepath.Join(t.TempDir(), "nope")}
	for _, target := range []string{"/", "/assets/app.abc.js", "/settings/prices"} {
		rec := do(t, srv, "GET", target)
		if rec.Code != 404 {
			t.Fatalf("构建产物缺失时 %s 状态码=%d，应为 404", target, rec.Code)
		}
		assertDetailNull(t, decodeJSON(t, rec.Body.Bytes()))
	}
}
