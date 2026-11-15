// subscription_plain_test.go 批 2 订阅测试：DELETE 级联（节点行/快照行/磁盘
// 产物/钩子）+ 明文导入（4 格式解析表测、非 plain 400 逐字、真拉取导入链、
// 失败落 FAILED 账）。
package proxypool

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"holdexar/server-go/internal/store"
)

func subPlainFixture(t *testing.T) (*sql.DB, *store.WriteGate, string, func(method, path, body string) *httptest.ResponseRecorder) {
	t.Helper()
	dir := t.TempDir()
	db, err := store.Open(filepath.Join(dir, "test.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	gate := store.NewWriteGate(db, filepath.Join(dir, "gate.lock"))
	if _, err := db.Exec(`CREATE TABLE app_settings (key VARCHAR(100) PRIMARY KEY, value_json TEXT, updated_at DATETIME);
	CREATE TABLE proxy_subscriptions (
		id INTEGER PRIMARY KEY AUTOINCREMENT, kind VARCHAR(16) NOT NULL, url VARCHAR(500) NOT NULL,
		label VARCHAR(100), created_at DATETIME, last_imported_at DATETIME, last_stats JSON,
		last_fetch_at DATETIME, last_fetch_status VARCHAR(16), last_success_at DATETIME,
		last_error VARCHAR(500), cache_url_identity VARCHAR(64), deprecated BOOLEAN DEFAULT 0,
		deprecated_at DATETIME, deprecated_reason VARCHAR(200), admission_status VARCHAR(16), auto_refresh BOOLEAN DEFAULT 1);
	CREATE TABLE subscription_snapshots (
		id INTEGER PRIMARY KEY AUTOINCREMENT, subscription_id INTEGER NOT NULL, sha256 VARCHAR(64) NOT NULL,
		format VARCHAR(16) NOT NULL, raw_path VARCHAR(500), url VARCHAR(500), node_count INTEGER NOT NULL,
		status VARCHAR(16) NOT NULL, http_status INTEGER, content_type VARCHAR(100), source_channel VARCHAR(32),
		source_egress VARCHAR(100), profile_title VARCHAR(100), content_disposition VARCHAR(255), userinfo VARCHAR(500),
		duration_ms INTEGER, attempts_json TEXT, provider_nodes_json TEXT, error TEXT, fetched_at DATETIME);
	CREATE TABLE health_observations (id INTEGER PRIMARY KEY AUTOINCREMENT, node_id VARCHAR(64), level VARCHAR(4));
	CREATE TABLE proxy_nodes (
		id INTEGER PRIMARY KEY AUTOINCREMENT, node_id VARCHAR(64) UNIQUE, runtime_name VARCHAR(400) UNIQUE,
		proxy_type VARCHAR(32), subscription_id INTEGER, state VARCHAR(16) NOT NULL, last_source_seen DATETIME,
		consecutive_failures INTEGER NOT NULL DEFAULT 0);
	CREATE TABLE proxies (
		id INTEGER PRIMARY KEY AUTOINCREMENT, label VARCHAR(100), scheme VARCHAR(16) NOT NULL,
		host VARCHAR(255) NOT NULL, port INTEGER NOT NULL, username VARCHAR(200), password VARCHAR(200),
		enabled BOOLEAN NOT NULL DEFAULT 1, status VARCHAR(16) NOT NULL DEFAULT 'unknown',
		latency_ms INTEGER, consecutive_failures INTEGER NOT NULL DEFAULT 0, last_checked_at DATETIME, created_at DATETIME);
	CREATE TABLE proxy_events (
		id INTEGER PRIMARY KEY AUTOINCREMENT, ts DATETIME, kind VARCHAR(20), target VARCHAR(255),
		proxy_label VARCHAR(100), status_code INTEGER, duration_ms INTEGER, error TEXT);`); err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	RegisterSubscriptionRoutes(mux, db, gate, dir, SubscriptionRouteOptions{
		AfterDelete:    func(_ context.Context, subID int64, _ string) { removedArtifacts = append(removedArtifacts, subID) },
		RevertToDirect: func(context.Context) { revertedDirect = true },
	})
	do := func(method, path, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, req)
		return rec
	}
	return db, gate, dir, do
}

var (
	removedArtifacts []int64
	revertedDirect   bool
)

func resetSubHooks() {
	removedArtifacts, revertedDirect = nil, false
}

func seedPlainSub(t *testing.T, db *sql.DB, kind string) int64 {
	t.Helper()
	var id int64
	if err := db.QueryRow(`INSERT INTO proxy_subscriptions(kind,url,created_at,admission_status) VALUES(?,?,datetime('now'),'ACTIVE') RETURNING id`,
		kind, "https://provider.example/sub/"+kind).Scan(&id); err != nil {
		t.Fatal(err)
	}
	return id
}

// TestParsePlainProxyLine 四格式表测（Python parse_plain_proxy_line 同径）。
func TestParsePlainProxyLine(t *testing.T) {
	cases := []struct {
		line string
		want map[string]any
	}{
		{"http://user:pass@10.0.0.1:8080", map[string]any{"scheme": "http", "host": "10.0.0.1", "port": 8080, "username": "user", "password": "pass"}},
		{"10.0.0.2:3128", map[string]any{"scheme": "http", "host": "10.0.0.2", "port": 3128, "username": nil, "password": nil}},
		{"10.0.0.3:3128:alice:secret", map[string]any{"scheme": "http", "host": "10.0.0.3", "port": 3128, "username": "alice", "password": "secret"}},
		{"bob:pw@10.0.0.4:9999", map[string]any{"scheme": "http", "host": "10.0.0.4", "port": 9999, "username": "bob", "password": "pw"}},
		{"# comment", nil},
		{"", nil},
		{"noscheme", nil},
		{"10.0.0.5:notaport", nil},
	}
	for _, tc := range cases {
		got := parsePlainProxyLine(tc.line)
		if tc.want == nil {
			if got != nil {
				t.Fatalf("%q 应 nil：%v", tc.line, got)
			}
			continue
		}
		if got == nil {
			t.Fatalf("%q 应解析成功", tc.line)
		}
		for key, want := range tc.want {
			switch key {
			case "username", "password":
				if want == nil {
					if ptr, _ := got[key].(*string); ptr != nil {
						t.Fatalf("%q %s 应 nil：%v", tc.line, key, got[key])
					}
				} else if text, _ := got[key].(*string); text == nil || *text != want.(string) {
					t.Fatalf("%q %s 不符：%v", tc.line, key, got[key])
				}
			default:
				if got[key] != want {
					t.Fatalf("%q %s 不符：%v != %v", tc.line, key, got[key], want)
				}
			}
		}
	}
}

// TestSubscriptionDeleteCascade clash 级联（退出生产池 + 快照行 + 目录 + 钩子）
// 与 plain 直删；缺席 404。
func TestSubscriptionDeleteCascade(t *testing.T) {
	resetSubHooks()
	db, _, dir, do := subPlainFixture(t)
	clashID := seedPlainSub(t, db, "clash")
	plainID := seedPlainSub(t, db, "plain")
	for _, id := range []int64{clashID, plainID} {
		if _, err := db.Exec(`INSERT INTO subscription_snapshots(subscription_id,sha256,format,url,node_count,status,fetched_at)
			VALUES(?,'sha'+?,'yaml','sealed',3,'OK',datetime('now'))`, id, id); err != nil {
			t.Fatal(err)
		}
		if _, err := db.Exec(`INSERT INTO proxy_nodes(node_id,runtime_name,proxy_type,subscription_id,state)
			VALUES(?,?, 'ss', ?, 'ACTIVE')`, "node-"+strconv.FormatInt(id, 10), "节点-"+strconv.FormatInt(id, 10), id); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.MkdirAll(filepath.Join(dir, "proxypool", "snapshots", strconv.FormatInt(clashID, 10)), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "proxypool", "snapshots", strconv.FormatInt(clashID, 10), "a.bin"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	rec := do("DELETE", "/api/v1/proxies/subscriptions/"+strconv.FormatInt(clashID, 10), "")
	if rec.Code != 200 || decodeSub(t, rec)["removed"] != true {
		t.Fatalf("clash 删除应 200：%d %s", rec.Code, rec.Body.String())
	}
	assertSubCounts(t, db, clashID, 0, 0, 0)
	if _, err := os.Stat(filepath.Join(dir, "proxypool", "snapshots", strconv.FormatInt(clashID, 10))); !os.IsNotExist(err) {
		t.Fatal("快照目录应整体回收")
	}
	if len(removedArtifacts) != 1 || removedArtifacts[0] != clashID {
		t.Fatalf("产物清理钩子应收到 clash id：%v", removedArtifacts)
	}
	if !revertedDirect {
		t.Fatal("删除后应触发回退直连钩子")
	}
	// plain 删除：不触生产池语义，同样清行与目录
	revertedDirect = false
	rec = do("DELETE", "/api/v1/proxies/subscriptions/"+strconv.FormatInt(plainID, 10), "")
	if rec.Code != 200 {
		t.Fatalf("plain 删除应 200：%d %s", rec.Code, rec.Body.String())
	}
	// plain 删除只删订阅行（快照/节点级联是 clash 专属语义）
	assertSubCounts(t, db, plainID, 0, 1, 1)
	rec = do("DELETE", "/api/v1/proxies/subscriptions/"+strconv.FormatInt(plainID, 10), "")
	if rec.Code != 404 || !strings.Contains(rec.Body.String(), "订阅不存在") {
		t.Fatalf("重复删除应 404：%d %s", rec.Code, rec.Body.String())
	}
	rec = do("DELETE", "/api/v1/proxies/subscriptions/abc", "")
	if rec.Code != 422 {
		t.Fatalf("非整路径应 422：%d", rec.Code)
	}
}

func queryIDs(t *testing.T, db *sql.DB, table string) []int64 {
	t.Helper()
	rows, err := db.Query("SELECT subscription_id FROM " + table)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var out []int64
	for rows.Next() {
		var id int64
		rows.Scan(&id)
		out = append(out, id)
	}
	return out
}

func assertSubCounts(t *testing.T, db *sql.DB, id int64, subs, snapshots, nodes int) {
	t.Helper()
	var subCount, snapCount, nodeCount int
	if err := db.QueryRow(`SELECT (SELECT COUNT(*) FROM proxy_subscriptions WHERE id=?),
		(SELECT COUNT(*) FROM subscription_snapshots WHERE subscription_id=?),
		(SELECT COUNT(*) FROM proxy_nodes WHERE subscription_id=?)`, id, id, id).
		Scan(&subCount, &snapCount, &nodeCount); err != nil {
		t.Fatal(err)
	}
	t.Logf("remaining snap subs=%v nodes=%v (want %d/%d/%d for id=%d)", queryIDs(t, db, "subscription_snapshots"), queryIDs(t, db, "proxy_nodes"), subs, snapshots, nodes, id)
	if subCount != subs || snapCount != snapshots || nodeCount != nodes {
		t.Fatalf("级联计数不符（sub=%d snap=%d node=%d）", subCount, snapCount, nodeCount)
	}
}

// TestImportPlainSubscription 非 plain 400 逐字 + 真拉取导入链 + 失败落账。
func TestImportPlainSubscription(t *testing.T) {
	resetSubHooks()
	db, gate, dir, do := subPlainFixture(t)
	clashID := seedPlainSub(t, db, "clash")
	rec := do("POST", "/api/v1/proxies/subscriptions/"+strconv.FormatInt(clashID, 10)+"/import", "")
	if rec.Code != 400 || !strings.Contains(rec.Body.String(), "该订阅不是明文代理订阅（kind=plain）") {
		t.Fatalf("非 plain 应 400 逐字：%d %s", rec.Code, rec.Body.String())
	}
	rec = do("POST", "/api/v1/proxies/subscriptions/999/import", "")
	if rec.Code != 400 || !strings.Contains(rec.Body.String(), "订阅不存在") {
		t.Fatalf("缺席应 400：%d %s", rec.Code, rec.Body.String())
	}
	// 真拉取导入：httptest 服务发明文行（四格式混排 + 注释 + 重复行）
	plainID := seedPlainSub(t, db, "plain")
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		_, _ = fmt.Fprint(w, "# pool\n10.9.9.9:20001:alice:pw\n10.9.9.9:20001:alice:pw\n10.9.9.10:20002\nbob:pw@10.9.9.11:20003\nhttp://carol:cpw@10.9.9.12:20004\n")
	}))
	defer server.Close()
	if _, err := db.Exec(`UPDATE proxy_subscriptions SET url=? WHERE id=?`, sealSubURL(t, dir, server.URL), plainID); err != nil {
		t.Fatal(err)
	}
	rec = do("POST", "/api/v1/proxies/subscriptions/"+strconv.FormatInt(plainID, 10)+"/import", "")
	if rec.Code != 200 {
		t.Fatalf("导入应 200：%d %s", rec.Code, rec.Body.String())
	}
	stats := decodeSub(t, rec)
	if stats["fetched"] != float64(5) || stats["added"] != float64(4) || stats["skipped"] != float64(1) {
		t.Fatalf("导入统计不符：%s", rec.Body.String())
	}
	var rowCount int
	if err := db.QueryRow(`SELECT COUNT(*) FROM proxies`).Scan(&rowCount); err != nil {
		t.Fatal(err)
	}
	if rowCount != 4 {
		t.Fatalf("应入库 4 行：%d", rowCount)
	}
	var sealedUser string
	if err := db.QueryRow(`SELECT username FROM proxies WHERE host='10.9.9.9'`).Scan(&sealedUser); err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(sealedUser, "enc1:") {
		t.Fatalf("凭据应密封：%q", sealedUser)
	}
	var imported sql.NullString
	var lastStats sql.NullString
	if err := db.QueryRow(`SELECT CAST(last_imported_at AS TEXT), CAST(last_stats AS TEXT) FROM proxy_subscriptions WHERE id=?`, plainID).
		Scan(&imported, &lastStats); err != nil {
		t.Fatal(err)
	}
	if !imported.Valid || imported.String == "" {
		t.Fatal("mark_imported 未落账")
	}
	if !strings.Contains(lastStats.String, `"added":4`) {
		t.Fatalf("last_stats 不符：%s", lastStats.String)
	}
	var eventCount int
	if err := db.QueryRow(`SELECT COUNT(*) FROM proxy_events WHERE kind='import' AND target=?`,
		fmt.Sprintf("plain:%d", plainID)).Scan(&eventCount); err != nil {
		t.Fatal(err)
	}
	if eventCount != 1 {
		t.Fatalf("走线事件应 1 条：%d", eventCount)
	}
	// 重复导入：全部按去重跳过
	rec = do("POST", "/api/v1/proxies/subscriptions/"+strconv.FormatInt(plainID, 10)+"/import", "")
	if rec.Code != 200 {
		t.Fatalf("重复导入应 200：%d %s", rec.Code, rec.Body.String())
	}
	stats = decodeSub(t, rec)
	if stats["added"] != float64(0) || stats["skipped"] != float64(5) {
		t.Fatalf("重复导入应全跳过：%s", rec.Body.String())
	}
	// 失败落 FAILED 账：不可达端口
	deadID := seedPlainSub(t, db, "plain")
	if _, err := db.Exec(`UPDATE proxy_subscriptions SET url=? WHERE id=?`, sealSubURL(t, dir, "http://127.0.0.1:1/none"), deadID); err != nil {
		t.Fatal(err)
	}
	rec = do("POST", "/api/v1/proxies/subscriptions/"+strconv.FormatInt(deadID, 10)+"/import", "")
	if rec.Code != 400 {
		t.Fatalf("拉取失败应 400：%d %s", rec.Code, rec.Body.String())
	}
	var fetchStatus string
	var snapshotFailed int
	if err := db.QueryRow(`SELECT last_fetch_status FROM proxy_subscriptions WHERE id=?`, deadID).Scan(&fetchStatus); err != nil {
		t.Fatal(err)
	}
	if fetchStatus != "FAILED" {
		t.Fatalf("订阅行应落 FAILED：%s", fetchStatus)
	}
	if err := db.QueryRow(`SELECT COUNT(*) FROM subscription_snapshots WHERE subscription_id=? AND status='FAILED'`, deadID).
		Scan(&snapshotFailed); err != nil {
		t.Fatal(err)
	}
	if snapshotFailed != 1 {
		t.Fatalf("失败留痕行缺失：%d", snapshotFailed)
	}
	_ = gate
	_ = dir
}

func sealSubURL(t *testing.T, dataDir, url string) string {
	t.Helper()
	sealed, err := SealURL(dataDir, url)
	if err != nil {
		t.Fatal(err)
	}
	return sealed
}

func decodeSub(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("响应不是 JSON：%v（%s）", err, rec.Body.String())
	}
	return out
}
