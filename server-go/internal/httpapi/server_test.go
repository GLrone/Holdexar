package httpapi

import (
	"encoding/json"
	"net/http/httptest"
	"os"
	"testing"
)

func newTestServer(t *testing.T) *Server {
	t.Helper()
	return &Server{App: "Holdexar", Version: "0.3.6-beta", DataDir: t.TempDir()}
}

// unsetForTest 恢复现场地删除单个环境变量（t.Setenv 只能设值不能删除）。
func unsetForTest(t *testing.T, key string) {
	t.Helper()
	saved, had := os.LookupEnv(key)
	if err := os.Unsetenv(key); err != nil {
		t.Fatalf("无法删除环境变量 %s：%v", key, err)
	}
	t.Cleanup(func() {
		if had {
			os.Setenv(key, saved)
		}
	})
}

func do(t *testing.T, srv *Server, method, target string) *httptest.ResponseRecorder {
	t.Helper()
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, httptest.NewRequest(method, target, nil))
	return rec
}

func decodeJSON(t *testing.T, body []byte) map[string]any {
	t.Helper()
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		t.Fatalf("响应体不是 JSON：%v（%s）", err, body)
	}
	return out
}

func assertDetailNull(t *testing.T, body map[string]any) {
	t.Helper()
	value, ok := body["detail"]
	if !ok || value != nil {
		t.Fatalf("错误体应为 {\"detail\": null}，实际 %v", body)
	}
	if len(body) != 1 {
		t.Fatalf("错误体应只有 detail 一个字段，实际 %v", body)
	}
}

func TestHealthDefaultShape(t *testing.T) {
	unsetForTest(t, "HOLDEXAR_UPDATE_ATTEMPT_ID")
	srv := newTestServer(t)
	rec := do(t, srv, "GET", "/api/v1/health")
	if rec.Code != 200 {
		t.Fatalf("健康端点状态码=%d，应为 200", rec.Code)
	}
	if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
		t.Fatalf("Content-Type=%s，应为 application/json", ct)
	}
	body := decodeJSON(t, rec.Body.Bytes())
	if len(body) != 7 {
		t.Fatalf("默认字段数=%d，应为 7：%v", len(body), body)
	}
	if body["status"] != "ok" {
		t.Fatalf("status=%v，应为 ok", body["status"])
	}
	if body["app"] != "Holdexar" || body["version"] != "0.3.6-beta" {
		t.Fatalf("app/version=%v/%v，应为品牌名与版本号", body["app"], body["version"])
	}
	pid, ok := body["pid"].(float64)
	if !ok || pid <= 0 {
		t.Fatalf("pid=%v，应为正整数", body["pid"])
	}
	if body["dataDir"] != srv.DataDir {
		t.Fatalf("dataDir=%v，应为解析后的数据目录", body["dataDir"])
	}
	if path, ok := body["executable"].(string); !ok || path == "" {
		t.Fatalf("executable=%v，应为可执行文件绝对路径", body["executable"])
	}
	if body["schedulerReady"] != false {
		t.Fatalf("schedulerReady=%v，骨架期应为 false", body["schedulerReady"])
	}
}

func TestHealthUpdateAttemptFields(t *testing.T) {
	t.Setenv("HOLDEXAR_UPDATE_ATTEMPT_ID", "attempt-1")
	srv := newTestServer(t)
	rec := do(t, srv, "GET", "/api/v1/health")
	if rec.Code != 200 {
		t.Fatalf("健康端点状态码=%d，应为 200", rec.Code)
	}
	body := decodeJSON(t, rec.Body.Bytes())
	if len(body) != 11 {
		t.Fatalf("更新交接期字段数=%d，应为 11：%v", len(body), body)
	}
	if body["updateAttemptId"] != "attempt-1" {
		t.Fatalf("updateAttemptId=%v，应为环境变量原文", body["updateAttemptId"])
	}
	if body["updateDataProtocol"] != float64(1) {
		t.Fatalf("updateDataProtocol=%v，应为 1", body["updateDataProtocol"])
	}
	if body["writeProtected"] != false || body["pageReady"] != false {
		t.Fatalf("保护位=%v/%v，缺省应为显式 false", body["writeProtected"], body["pageReady"])
	}
}

func TestHealthMethodNotAllowed(t *testing.T) {
	srv := newTestServer(t)
	rec := do(t, srv, "POST", "/api/v1/health")
	if rec.Code != 405 {
		t.Fatalf("POST 健康端点状态码=%d，应为 405", rec.Code)
	}
	if allow := rec.Header().Get("Allow"); allow != "GET, HEAD" {
		t.Fatalf("Allow=%s，应为 GET, HEAD", allow)
	}
	assertDetailNull(t, decodeJSON(t, rec.Body.Bytes()))
}

func TestUnknownAPIPath(t *testing.T) {
	srv := newTestServer(t)
	for _, method := range []string{"GET", "POST", "DELETE"} {
		rec := do(t, srv, method, "/api/v1/nope")
		if rec.Code != 404 {
			t.Fatalf("%s 未知 API 状态码=%d，应为 404", method, rec.Code)
		}
		if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
			t.Fatalf("Content-Type=%s，应为 application/json", ct)
		}
		assertDetailNull(t, decodeJSON(t, rec.Body.Bytes()))
	}
}
