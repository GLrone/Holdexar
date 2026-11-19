package proxypool

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"holdexar/server-go/internal/store"
)

func userProxyFixture(t *testing.T) (*sql.DB, *store.WriteGate, string, func(method, path, body string) *httptest.ResponseRecorder) {
	t.Helper()
	dir := t.TempDir()
	db, err := store.Open(filepath.Join(dir, "test.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	gate := store.NewWriteGate(db, filepath.Join(dir, "gate.lock"))
	if _, err := db.Exec(`CREATE TABLE app_settings (key VARCHAR(100) PRIMARY KEY, value_json TEXT, updated_at DATETIME);
	CREATE TABLE proxies (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		label VARCHAR(100), scheme VARCHAR(16) NOT NULL, host VARCHAR(255) NOT NULL, port INTEGER NOT NULL,
		username VARCHAR(200), password VARCHAR(200), enabled BOOLEAN NOT NULL DEFAULT 1,
		status VARCHAR(16) NOT NULL DEFAULT 'unknown', latency_ms INTEGER,
		consecutive_failures INTEGER NOT NULL DEFAULT 0, last_checked_at DATETIME, created_at DATETIME);`); err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	RegisterUserProxyRoutes(mux, db, gate, dir, UserProxyOptions{})
	do := func(method, path, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, req)
		return rec
	}
	return db, gate, dir, do
}

func TestAddProxyShapes(t *testing.T) {
	_, _, _, do := userProxyFixture(t)
	rec := do("POST", "/api/v1/proxies", `{"url": "http://user:pass@127.0.0.1:8080", "label": "L1"}`)
	if rec.Code != 200 {
		t.Fatalf("单条应 200：%d %s", rec.Code, rec.Body.String())
	}
	body := decodeUserProxy(t, rec)
	if body["added"] != float64(1) {
		t.Fatal(body)
	}
	item := body["items"].([]any)[0].(map[string]any)
	if item["hasAuth"] != true || item["url"] != "http://***@127.0.0.1:8080" || item["status"] != "unknown" {
		t.Fatalf("掩码投影不符：%v", item)
	}
	if item["label"] != "L1" {
		t.Fatalf("label 不符：%v", item)
	}
	rec = do("POST", "/api/v1/proxies", `{"url": "https://127.0.0.2:8443\n\n127.0.0.3:9090"}`)
	body = decodeUserProxy(t, rec)
	if body["added"] != float64(2) {
		t.Fatalf("批量应 2 条（https 归一 + 补 scheme）：%s", rec.Body.String())
	}
	items := body["items"].([]any)
	if items[0].(map[string]any)["scheme"] != "http" || items[1].(map[string]any)["port"] != float64(9090) {
		t.Fatalf("批量解析不符：%v", items)
	}
	for _, tc := range []struct{ body, text string }{
		{`{"url": "not a proxy"}`, "无法解析代理地址: not a proxy"},
		{`{}`, "host/port 不能为空"},
		{`{"host": "h", "port": 1, "scheme": "ftp"}`, "不支持的协议: ftp"},
		{`{"url": "http://127.0.0.4"}`, "无法解析代理地址: http://127.0.0.4"},
	} {
		rec := do("POST", "/api/v1/proxies", tc.body)
		if rec.Code != 400 || !strings.Contains(rec.Body.String(), tc.text) {
			t.Fatalf("应 400 %s：%d %s", tc.text, rec.Code, rec.Body.String())
		}
	}
	rec = do("POST", "/api/v1/proxies", `{"host": "h", "port": "abc"}`)
	if rec.Code != 422 {
		t.Fatalf("port 非整应 422：%d", rec.Code)
	}
	detail, _ := decodeUserProxy(t, rec)["detail"].([]any)
	first := detail[0].(map[string]any)
	if first["type"] != "int_parsing" {
		t.Fatal(first)
	}
}

func TestUpdateAndDeleteProxy(t *testing.T) {
	db, _, _, do := userProxyFixture(t)
	rec := do("POST", "/api/v1/proxies", `{"url": "socks5://127.0.0.1:1080"}`)
	if rec.Code != 200 {
		t.Fatalf("ADD 应 200：%d %s", rec.Code, rec.Body.String())
	}
	id := int64(decodeUserProxy(t, rec)["items"].([]any)[0].(map[string]any)["id"].(float64))
	t.Logf("ADD id=%d path=%s", id, "/api/v1/proxies/"+utoa(id))
	if _, err := db.Exec(`UPDATE proxies SET consecutive_failures=3, status='failed' WHERE id=?`, id); err != nil {
		t.Fatal(err)
	}
	rec = do("PUT", "/api/v1/proxies/"+utoa(id), `{"enabled": false}`)
	if rec.Code != 200 {
		t.Fatalf("PUT 应 200：%d %s", rec.Code, rec.Body.String())
	}
	view := decodeUserProxy(t, rec)
	if view["enabled"] != false || view["consecutiveFailures"] != float64(0) || view["status"] != "failed" {
		t.Fatalf("enabled 复位语义不符：%s", rec.Body.String())
	}
	var stored bool
	if err := db.QueryRow(`SELECT enabled FROM proxies WHERE id=?`, id).Scan(&stored); err != nil || stored {
		t.Fatalf("库内 enabled 应 false：%v %v", stored, err)
	}
	rec = do("PUT", "/api/v1/proxies/999", `{"enabled": true}`)
	if rec.Code != 404 || !strings.Contains(rec.Body.String(), "代理不存在") {
		t.Fatalf("缺席应 404：%d %s", rec.Code, rec.Body.String())
	}
	rec = do("DELETE", "/api/v1/proxies/"+utoa(id), "")
	if rec.Code != 200 || decodeUserProxy(t, rec)["removed"] != true {
		t.Fatalf("DELETE 应 200 removed：%d %s", rec.Code, rec.Body.String())
	}
	rec = do("DELETE", "/api/v1/proxies/"+utoa(id), "")
	if rec.Code != 404 || !strings.Contains(rec.Body.String(), "代理不存在") {
		t.Fatalf("重复 DELETE 应 404：%d %s", rec.Code, rec.Body.String())
	}
	rec = do("DELETE", "/api/v1/proxies/abc", "")
	if rec.Code != 422 {
		t.Fatalf("非整路径应 422：%d", rec.Code)
	}
	detail, _ := decodeUserProxy(t, rec)["detail"].([]any)
	if detail[0].(map[string]any)["type"] != "int_parsing" {
		t.Fatal(rec.Body.String())
	}
}

func utoa(id int64) string {
	return strconv.FormatInt(id, 10)
}

func decodeUserProxy(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("响应不是 JSON：%v（%s）", err, rec.Body.String())
	}
	return out
}

func toString(value any) string {
	if text, ok := value.(string); ok {
		return text
	}
	return fmt.Sprint(value)
}

func writeKV(t *testing.T, db *sql.DB, key, strategy string) {
	t.Helper()
	if _, err := db.Exec(`INSERT INTO app_settings(key,value_json,updated_at) VALUES(?,?,datetime('now'))
		ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json`, key, `"`+strategy+`"`); err != nil {
		t.Fatal(err)
	}
}

func TestResolveStrategies(t *testing.T) {
	db, _, _, do := userProxyFixture(t)
	setStrategy := func(strategy string) {
		t.Helper()
		writeKV(t, db, "proxy.strategy", strategy)
	}
	rec := do("GET", "/api/v1/proxies/resolve", "")
	if rec.Code != 200 {
		t.Fatalf("空策略应 200：%d %s", rec.Code, rec.Body.String())
	}
	setStrategy("proxy_only")
	rec = do("GET", "/api/v1/proxies/resolve", "")
	if rec.Code != 200 {
		t.Fatalf("proxy_only 无节点应 200：%d %s", rec.Code, rec.Body.String())
	}
	body := decodeUserProxy(t, rec)
	if body["proxyUrl"] != nil || !strings.Contains(toString(body["message"]), "策略 proxy_only 需要至少一条启用的代理") {
		t.Fatalf("RuntimeError 语义不符：%s", rec.Body.String())
	}
	setStrategy("direct_first")
	rec = do("GET", "/api/v1/proxies/resolve", "")
	body = decodeUserProxy(t, rec)
	if body["proxyUrl"] != nil {
		t.Fatalf("direct_first 应直连：%s", rec.Body.String())
	}
	setStrategy("proxy_first")
	doAdd := func(port int64) {
		rec := do("POST", "/api/v1/proxies", `{"host": "127.0.0.1", "port": `+utoa(port)+`, "scheme": "http"}`)
		if rec.Code != 200 {
			t.Fatalf("播种失败：%s", rec.Body.String())
		}
	}
	doAdd(18080)
	doAdd(18081)
	if _, err := db.Exec(`UPDATE proxies SET status='failed', consecutive_failures=5`); err != nil {
		t.Fatal(err)
	}
	rec = do("GET", "/api/v1/proxies/resolve", "")
	body = decodeUserProxy(t, rec)
	if body["proxyUrl"] != "http://127.0.0.1:18080" {
		t.Fatalf("轮询首拍应第一节点：%s", rec.Body.String())
	}
	rec = do("GET", "/api/v1/proxies/resolve", "")
	body = decodeUserProxy(t, rec)
	if body["proxyUrl"] != "http://127.0.0.1:18081" {
		t.Fatalf("轮询次拍应第二节点：%s", rec.Body.String())
	}
	var rrIndex int
	if err := db.QueryRow(`SELECT value_json FROM app_settings WHERE key='proxy.rr_index'`).Scan(&rrIndex); err != nil {
		t.Fatalf("rr_index 未记账：%v", err)
	}
	if rrIndex != 2 {
		t.Fatalf("rr_index 应为 2：%d", rrIndex)
	}
	if _, err := db.Exec(`UPDATE proxies SET status='ok', consecutive_failures=0, latency_ms=120`); err != nil {
		t.Fatal(err)
	}
	rec = do("GET", "/api/v1/proxies/resolve", "")
	body = decodeUserProxy(t, rec)
	url := toString(body["proxyUrl"])
	if url != "http://127.0.0.1:18080" && url != "http://127.0.0.1:18081" {
		t.Fatalf("加权随机应取池节点：%v", body)
	}
}

func TestResolveStrategyProxyProgrammatic(t *testing.T) {
	db, gate, dir, do := userProxyFixture(t)
	writeKV(t, db, "proxy.strategy", "proxy_first")
	savedPorts := localMixedPorts
	localMixedPorts = nil
	t.Cleanup(func() { localMixedPorts = savedPorts })
	proxyURL, message, err := ResolveStrategyProxy(context.Background(), db, gate, dir, nil)
	if err != nil || proxyURL != "" || message != "" {
		t.Fatalf("内核未起且无兜底口应直连：%q %q %v", proxyURL, message, err)
	}
	rec := do("POST", "/api/v1/proxies", `{"host": "127.0.0.1", "port": 18080, "scheme": "http"}`)
	if rec.Code != 200 {
		t.Fatalf("播种失败：%s", rec.Body.String())
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { listener.Close() })
	port := listener.Addr().(*net.TCPAddr).Port
	proxyURL, _, err = ResolveStrategyProxy(context.Background(), db, gate, dir, func() (bool, int) { return true, port })
	if err != nil || proxyURL != fmt.Sprintf("http://127.0.0.1:%d", port) {
		t.Fatalf("内核在跑应取内核口：%q %v", proxyURL, err)
	}
	proxyURL, _, err = ResolveStrategyProxy(context.Background(), db, gate, dir, nil)
	if err != nil || proxyURL != "http://127.0.0.1:18080" {
		t.Fatalf("内核未起应取池节点：%q %v", proxyURL, err)
	}
}

func TestRuntimeURLUsesPercent20UserinfoEncoding(t *testing.T) {
	dir := t.TempDir()
	su, err := sealProxyCredential(dir, "my user+1")
	if err != nil {
		t.Fatal(err)
	}
	sp, err := sealProxyCredential(dir, "p@ss word")
	if err != nil {
		t.Fatal(err)
	}
	got := runtimeURL(dir, proxyRow{
		Scheme:   "http",
		Host:     "10.0.0.1",
		Port:     8080,
		Username: sql.NullString{String: su, Valid: true},
		Password: sql.NullString{String: sp, Valid: true},
	})
	want := "http://my%20user%2B1:p%40ss%20word@10.0.0.1:8080"
	if got != want {
		t.Fatalf("runtimeURL = %q, want %q", got, want)
	}
}
