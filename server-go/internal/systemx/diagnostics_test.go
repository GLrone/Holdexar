package systemx

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"path/filepath"
	"strings"
	"testing"

	"holdexar/server-go/internal/eventstream"
	"holdexar/server-go/internal/netenv"
	"holdexar/server-go/internal/store"
)

func diagFixture(t *testing.T, tables bool) (*sql.DB, *eventstream.Ring, *http.ServeMux) {
	t.Helper()
	cleanup := netenv.ResetTakeoverForTest(func(_ context.Context, _, _ string) ([]netip.Addr, error) {
		return []netip.Addr{netip.MustParseAddr("1.2.3.4")}, nil
	})
	t.Cleanup(cleanup)
	dir := t.TempDir()
	path := filepath.Join(dir, "holdexar.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if tables {
		if _, err := db.Exec(`CREATE TABLE app_settings (key VARCHAR(100) PRIMARY KEY, value_json TEXT, updated_at DATETIME);
		CREATE TABLE proxies (id INTEGER PRIMARY KEY, enabled BOOLEAN, status VARCHAR(16));
		CREATE TABLE game_current_prices (appid BIGINT, region_code VARCHAR(8), price_status VARCHAR(16), updated_at DATETIME);`); err != nil {
			t.Fatal(err)
		}
	}
	ring := eventstream.NewRing(64)
	mux := http.NewServeMux()
	RegisterDiagnostics(mux, db, DiagnosticsDeps{
		Gate: store.NewWriteGate(db, filepath.Join(dir, "gate.lock")),
		Ring: ring,
	})
	do := func(method, path string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, path, nil)
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, req)
		return rec
	}
	_ = do
	return db, ring, mux
}

func diagDo(mux *http.ServeMux, method, path string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, path, nil)
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, req)
	return rec
}

func TestSystemLogsLimitClamp(t *testing.T) {
	_, ring, mux := diagFixture(t, true)
	for i := 0; i < 5; i++ {
		ring.Write("2026-11-14 10:00:0" + string(rune('0'+i)) + " [信息] 服务端 · 行" + string(rune('0'+i)))
	}
	rec := diagDo(mux, "GET", "/api/v1/system/logs")
	if rec.Code != 200 {
		t.Fatalf("默认应 200：%d", rec.Code)
	}
	var payload struct {
		Lines []string `json:"lines"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	if len(payload.Lines) != 5 {
		t.Fatalf("应回放 5 行：%d", len(payload.Lines))
	}
	if !strings.Contains(payload.Lines[0], "行0") {
		t.Fatalf("快照应旧→新：%q", payload.Lines[0])
	}
	rec = diagDo(mux, "GET", "/api/v1/system/logs?limit=2")
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	if len(payload.Lines) != 2 || !strings.Contains(payload.Lines[0], "行3") {
		t.Fatalf("limit 夹取应取最近 2 行：%s", rec.Body.String())
	}
	rec = diagDo(mux, "GET", "/api/v1/system/logs?limit=0")
	if rec.Code != 422 {
		t.Fatalf("limit=0 应 422：%d", rec.Code)
	}
	var body struct {
		Detail []struct {
			Type string         `json:"type"`
			Ctx  map[string]any `json:"ctx"`
		} `json:"detail"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body.Detail[0].Type != "greater_than_equal" || body.Detail[0].Ctx["ge"] != float64(1) {
		t.Fatal(rec.Body.String())
	}
	rec = diagDo(mux, "GET", "/api/v1/system/logs?limit=2001")
	var body2 struct {
		Detail []struct {
			Type string `json:"type"`
		} `json:"detail"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body2); err != nil {
		t.Fatal(err)
	}
	if body2.Detail[0].Type != "less_than_equal" {
		t.Fatal(rec.Body.String())
	}
}

func TestWriteSchedulerSnapshot(t *testing.T) {
	_, _, mux := diagFixture(t, true)
	rec := diagDo(mux, "GET", "/api/v1/system/write-scheduler")
	if rec.Code != 200 {
		t.Fatalf("应 200：%d %s", rec.Code, rec.Body.String())
	}
	body := decodeDiag(t, rec)
	for _, key := range []string{"busy", "depth", "owner_priority", "owner_label",
		"waiting_interactive", "waiting_background", "pool"} {
		if _, ok := body[key]; !ok {
			t.Fatalf("缺键 %s：%s", key, rec.Body.String())
		}
	}
	pool, ok := body["pool"].(map[string]any)
	if !ok {
		t.Fatal("pool 段缺失")
	}
	for _, key := range []string{"type", "status", "size", "checkedOut", "checkedIn", "overflow"} {
		if _, ok := pool[key]; !ok {
			t.Fatalf("pool 缺键 %s：%s", key, rec.Body.String())
		}
	}
}

func decodeDiag(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("响应不是 JSON：%v（%s）", err, rec.Body.String())
	}
	return out
}

func TestDiagnosticsFiveSections(t *testing.T) {
	db, ring, mux := diagFixture(t, true)
	if _, err := db.Exec(`INSERT INTO app_settings(key,value_json,updated_at) VALUES('proxy.strategy','"proxy_first"','2026-11-14 10:00:00')`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO proxies(id,enabled,status) VALUES(1,1,'ok'),(2,1,'unknown')`); err != nil {
		t.Fatal(err)
	}
	ring.Write("12:00:01 [信息] 服务端 · 写库分段完成 │ 款数=12 总计=88毫秒 等写锁=5毫秒")
	rec := diagDo(mux, "GET", "/api/v1/system/diagnostics")
	if rec.Code != 200 {
		t.Fatalf("应 200：%d %s", rec.Code, rec.Body.String())
	}
	body := decodeDiag(t, rec)
	for _, key := range []string{"generated_at", "identity", "cycles", "job_runs", "write_ledger", "backlog", "signals"} {
		if _, ok := body[key]; !ok {
			t.Fatalf("缺段 %s：%s", key, rec.Body.String())
		}
	}
	identity := body["identity"].(map[string]any)
	if identity["strategy"] != "proxy_first" || identity["strategy_explicit"] != false {
		t.Fatal(identity)
	}
	if _, ok := identity["schema_version"]; !ok {
		t.Fatal("identity 缺 schema_version")
	}
	writeLedger := body["write_ledger"].(map[string]any)
	samples := writeLedger["samples"].([]any)
	if len(samples) != 1 {
		t.Fatalf("写账样本应有 1 条：%s", rec.Body.String())
	}
	sample := samples[0].(map[string]any)
	if sample["message"] != "写库分段完成" || sample["entries"] != float64(12) || sample["total_ms"] != float64(88) || sample["lockwait_ms"] != float64(5) {
		t.Fatalf("写账样本解析不符：%s", rec.Body.String())
	}
	if body["backlog"].(map[string]any)["missing_pending"] != false {
		t.Fatal("空库不应有补抓欠账")
	}
	if len(body["signals"].([]any)) != 0 {
		t.Fatalf("健康态不应有信号：%s", rec.Body.String())
	}
}

func TestDiagnosticsSectionFailure(t *testing.T) {
	_, _, mux := diagFixture(t, false)
	rec := diagDo(mux, "GET", "/api/v1/system/diagnostics")
	if rec.Code != 200 {
		t.Fatalf("诊断面自身必须可用：%d %s", rec.Code, rec.Body.String())
	}
	body := decodeDiag(t, rec)
	if _, ok := body["backlog"].(map[string]any)["error"]; !ok {
		t.Fatalf("backlog 段应降级为 error：%s", rec.Body.String())
	}
	if _, ok := body["cycles"].(map[string]any)["error"]; !ok {
		t.Fatalf("cycles 段应降级为 error：%s", rec.Body.String())
	}
	identity := body["identity"].(map[string]any)
	if _, ok := identity["strategy"]; !ok {
		t.Fatalf("identity 段键集必须完整：%v", identity)
	}
	if _, ok := body["generated_at"]; !ok {
		t.Fatal("聚合壳必须完整")
	}
	if len(body["signals"].([]any)) != 0 {
		t.Fatal("失败面不得产信号")
	}
}

func TestDiagnosticsDirectDepsAndSignals(t *testing.T) {
	cleanup := netenv.ResetTakeoverForTest(func(_ context.Context, _, _ string) ([]netip.Addr, error) {
		return []netip.Addr{netip.MustParseAddr("1.2.3.4")}, nil
	})
	t.Cleanup(cleanup)

	dir := t.TempDir()
	db, err := store.Open(filepath.Join(dir, "holdexar.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if _, err := db.Exec(`CREATE TABLE app_settings (key VARCHAR(100) PRIMARY KEY, value_json TEXT, updated_at DATETIME);
	CREATE TABLE proxies (id INTEGER PRIMARY KEY, enabled BOOLEAN, status VARCHAR(16));
	CREATE TABLE game_current_prices (appid BIGINT, region_code VARCHAR(8), price_status VARCHAR(16), updated_at DATETIME);`); err != nil {
		t.Fatal(err)
	}

	mux := http.NewServeMux()
	RegisterDiagnostics(mux, db, DiagnosticsDeps{
		Gate: store.NewWriteGate(db, filepath.Join(dir, "gate.lock")),
		Ring: eventstream.NewRing(16),
		Cycles: func(_ context.Context, limit int) ([]map[string]any, error) {
			if limit != 5 {
				t.Fatalf("expected limit 5, got %d", limit)
			}
			return []map[string]any{{"id": 7, "status": "failed", "error": "网络超时"}}, nil
		},
		JobRuns: func(_ context.Context, limit int) (map[string]any, error) {
			if limit != 10 {
				t.Fatalf("expected limit 10, got %d", limit)
			}
			return map[string]any{
				"total": 2,
				"items": []any{
					map[string]any{"id": 1, "status": "failed"},
					map[string]any{"id": 2, "status": "success"},
				},
			}, nil
		},
	})

	rec := diagDo(mux, "GET", "/api/v1/system/diagnostics")
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	body := decodeDiag(t, rec)
	cycles := body["cycles"].(map[string]any)
	if cycles["last"].(map[string]any)["status"] != "failed" {
		t.Fatalf("unexpected cycles section: %v", cycles)
	}
	jobRuns := body["job_runs"].(map[string]any)
	if jobRuns["total"] != float64(2) {
		t.Fatalf("unexpected job_runs section: %v", jobRuns)
	}
	signals := body["signals"].([]any)
	if len(signals) != 2 {
		t.Fatalf("expected 2 signals (last_cycle_failed + job_runs_failed), got %v", signals)
	}
}
