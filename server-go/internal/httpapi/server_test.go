package httpapi

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"

	"holdexar/server-go/internal/access"
)

func newTestServer(t *testing.T) *Server {
	t.Helper()
	return &Server{App: "Holdexar", Version: "0.3.6-beta", DataDir: t.TempDir()}
}

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

func doFrom(t *testing.T, srv *Server, method, target, remoteAddr string) *httptest.ResponseRecorder {
	t.Helper()
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(method, target, nil)
	req.RemoteAddr = remoteAddr
	srv.Handler().ServeHTTP(rec, req)
	return rec
}

func TestHealthDefaultShape(t *testing.T) {
	unsetForTest(t, "HOLDEXAR_UPDATE_ATTEMPT_ID")
	srv := newTestServer(t)
	rec := doFrom(t, srv, "GET", "/api/v1/health", "127.0.0.1:51234")
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

func TestHealthRemotePeerMinimal(t *testing.T) {
	unsetForTest(t, "HOLDEXAR_UPDATE_ATTEMPT_ID")
	gate := access.New()
	srv := newTestServer(t)
	srv.Gate = gate

	rec := doFrom(t, srv, "GET", "/api/v1/health", "192.168.1.5:51234")
	body := decodeJSON(t, rec.Body.Bytes())
	if len(body) != 1 || body["status"] != "ok" {
		t.Fatalf("未鉴权远端应只返回 status:ok，实际=%v", body)
	}

	t.Setenv("HOLDEXAR_UPDATE_ATTEMPT_ID", "attempt-secret")
	rec = doFrom(t, srv, "GET", "/api/v1/health", "192.168.1.5:51234")
	body = decodeJSON(t, rec.Body.Bytes())
	if len(body) != 1 || body["status"] != "ok" {
		t.Fatalf("更新期间未鉴权远端不得泄露字段，实际=%v", body)
	}
	unsetForTest(t, "HOLDEXAR_UPDATE_ATTEMPT_ID")

	w := httptest.NewRecorder()
	gate.Issue(w)
	cookie := w.Result().Cookies()[0]
	authRec := httptest.NewRecorder()
	authReq := httptest.NewRequest(http.MethodGet, "/api/v1/health", nil)
	authReq.RemoteAddr = "192.168.1.5:51234"
	authReq.AddCookie(cookie)
	srv.Handler().ServeHTTP(authRec, authReq)
	held := decodeJSON(t, authRec.Body.Bytes())
	if len(held) != 4 {
		t.Fatalf("持会话远端字段数=%d，应为 4：%v", len(held), held)
	}
	for _, key := range []string{"pid", "dataDir", "executable"} {
		if _, present := held[key]; present {
			t.Fatalf("持会话远端不得含 %s", key)
		}
	}
	if held["status"] != "ok" || held["app"] != "Holdexar" || held["version"] != "0.3.6-beta" {
		t.Fatalf("持会话远端最小面不符：%v", held)
	}

	v6Rec := doFrom(t, srv, "GET", "/api/v1/health", "[::1]")
	v6Body := decodeJSON(t, v6Rec.Body.Bytes())
	if len(v6Body) != 7 || v6Body["dataDir"] != srv.DataDir {
		t.Fatalf("IPv6 回环未返回完整快照：%v", v6Body)
	}
}

func TestWriteJSONNoEscapeHTMLAndNoTrailingNewline(t *testing.T) {
	rec := httptest.NewRecorder()
	writeJSON(rec, http.StatusOK, map[string]string{"text": "a&b<c>d"})
	got := rec.Body.Bytes()
	want := []byte(`{"text":"a&b<c>d"}`)
	if !bytes.Equal(got, want) {
		t.Fatalf("writeJSON=%q, want %q", got, want)
	}
}

func TestHealthUpdateAttemptFields(t *testing.T) {
	t.Setenv("HOLDEXAR_UPDATE_ATTEMPT_ID", "attempt-1")
	srv := newTestServer(t)
	rec := doFrom(t, srv, "GET", "/api/v1/health", "127.0.0.1:51234")
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

func TestHealthAuthenticatedLANHidesUpdateFieldsAndUsesDynamicCallbacks(t *testing.T) {
	t.Setenv("HOLDEXAR_UPDATE_ATTEMPT_ID", "attempt-lan-secret")
	gate := access.New()
	srv := newTestServer(t)
	srv.Gate = gate
	schedReady := true
	writeProtected := true
	srv.SchedulerReadyFn = func() bool { return schedReady }
	srv.UpdateWriteProtectedFn = func() bool { return writeProtected }

	w := httptest.NewRecorder()
	gate.Issue(w)
	cookie := w.Result().Cookies()[0]

	lanRec := httptest.NewRecorder()
	lanReq := httptest.NewRequest(http.MethodGet, "/api/v1/health", nil)
	lanReq.RemoteAddr = "192.168.1.25:55555"
	lanReq.AddCookie(cookie)
	srv.Handler().ServeHTTP(lanRec, lanReq)
	if lanRec.Code != http.StatusOK {
		t.Fatalf("持会话局域网对端状态码=%d，应为 200", lanRec.Code)
	}
	lanBody := decodeJSON(t, lanRec.Body.Bytes())
	for _, hidden := range []string{"updateAttemptId", "updateDataProtocol", "writeProtected", "pageReady", "pid", "dataDir", "executable"} {
		if _, exists := lanBody[hidden]; exists {
			t.Fatalf("持会话局域网对端不得暴露 %s：%v", hidden, lanBody)
		}
	}
	if lanBody["schedulerReady"] != true {
		t.Fatalf("SchedulerReadyFn 未生效：%v", lanBody)
	}

	loopRec := doFrom(t, srv, "GET", "/api/v1/health", "127.0.0.1:55555")
	loopBody := decodeJSON(t, loopRec.Body.Bytes())
	if loopBody["updateAttemptId"] != "attempt-lan-secret" {
		t.Fatalf("回环对端应返回 updateAttemptId，实际=%v", loopBody)
	}
	if loopBody["writeProtected"] != true || loopBody["schedulerReady"] != true {
		t.Fatalf("动态回调保护位/调度位未生效：%v", loopBody)
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
		body := decodeJSON(t, rec.Body.Bytes())
		if body["detail"] != "Not Found" {
			t.Fatalf("未知 API 404 文案=%v，应为 Not Found", body["detail"])
		}
	}
}
