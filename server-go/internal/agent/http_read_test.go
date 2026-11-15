// http_read_test.go agent 读面五端点测试：tasks 静态四项、runs 三源合并投影、
// events 分页、cancel 幂等矩阵（形状对齐 Python agent/router.py venv 实测锚点）。
package agent

import (
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"holdexar/server-go/internal/store"
)

const readSchema = reconcileSchema + `
CREATE TABLE price_cycles (
	id INTEGER PRIMARY KEY, kind VARCHAR(20), status VARCHAR(20), batches_done INTEGER,
	batches_expected INTEGER, units_failed INTEGER, started_at DATETIME, finished_at DATETIME, error TEXT);
CREATE TABLE crawl_jobs (
	id INTEGER PRIMARY KEY AUTOINCREMENT, kind VARCHAR(20), status VARCHAR(20), cycle_id INTEGER,
	stats_json JSON, started_at DATETIME, finished_at DATETIME, error TEXT);
`

func readFixtureDB(t *testing.T) (*sql.DB, *store.WriteGate) {
	t.Helper()
	db, gate := newReconcileDB(t)
	if _, err := db.Exec(readSchema[strings.Index(readSchema, "CREATE TABLE price_cycles"):]); err != nil {
		t.Fatalf("域表建表失败: %v", err)
	}
	return db, gate
}

func readMux(t *testing.T, db *sql.DB, deps *Deps) func(method, path, body string) *httptest.ResponseRecorder {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/v1/crawl/active", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"activeJobId": 7, "busy": true})
	})
	mux.HandleFunc("POST /api/v1/crawl/stop", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"stopped": true})
	})
	RegisterHTTPRead(mux, db, deps)
	return func(method, path, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, req)
		return rec
	}
}

func decodeRead(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("响应不是 JSON：%v（%s）", err, rec.Body.String())
	}
	return out
}

// TestAgentTasksStatic 四项固定名称与顺序。
func TestAgentTasksStatic(t *testing.T) {
	do := readMux(t, nil, &Deps{})
	rec := do("GET", "/api/v1/agent/tasks", "")
	if rec.Code != 200 {
		t.Fatalf("应 200：%d", rec.Code)
	}
	var items []map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &items); err != nil {
		t.Fatal(err)
	}
	want := []struct{ kind, label string }{
		{"price_refresh", "priceRefresh"}, {"price_repair", "priceRepair"},
		{"price_missing", "priceMissing"}, {"dev_pytest", "devPytest"},
	}
	if len(items) != len(want) {
		t.Fatalf("应四项：%s", rec.Body.String())
	}
	for i, item := range items {
		if item["kind"] != want[i].kind || item["label"] != want[i].label {
			t.Fatalf("第 %d 项不符：%v", i, item)
		}
	}
}

// TestAgentRunsMerge 三源合并：账本行过滤（task 例外与 job 价格任务排除）、
// 域行投影、排序与截断。
func TestAgentRunsMerge(t *testing.T) {
	db, _ := readFixtureDB(t)
	seed := func(q string, args ...any) {
		t.Helper()
		if _, err := db.Exec(q, args...); err != nil {
			t.Fatal(err)
		}
	}
	seed(`INSERT INTO agent_runs(id,session_id,runner,"trigger",meta,status,created_at,started_at,finished_at)
		VALUES('f1','sess','fake','manual','{}','running','2026-11-14 09:00:00','2026-11-14 09:00:00',NULL),
		('j1','sess','job','scheduled','{"task":"price_refresh"}','done','2026-11-14 08:00:00',NULL,NULL),
		('j2','sess','job','scheduled','{"job":"pool_cycle"}','done','2026-11-14 07:00:00',NULL,NULL),
		('t1','sess','task','manual','{"task":"dev_pytest"}','done','2026-11-14 06:00:00',NULL,NULL)`)
	seed(`INSERT INTO price_cycles(id,kind,status,batches_done,batches_expected,units_failed,started_at,finished_at,error)
		VALUES(9,'scheduled','completed',4,5,2,'2026-11-14 10:00:00','2026-11-14 11:00:00','boom')`)
	seed(`INSERT INTO crawl_jobs(id,kind,status,cycle_id,stats_json,started_at,finished_at,error)
		VALUES(7,'repair','done',NULL,'{"trigger":"manual","processed":3,"total":9,"success":2}','2026-11-14 12:00:00','2026-11-14 12:30:00','lastfail')`)
	do := readMux(t, db, &Deps{})
	rec := do("GET", "/api/v1/agent/runs", "")
	if rec.Code != 200 {
		t.Fatalf("应 200：%d %s", rec.Code, rec.Body.String())
	}
	body := decodeRead(t, rec)
	items := body["items"].([]any)
	// f1(fake) / cycle:9 / crawl:7 / j2(job 非价格任务) / t1(dev_pytest)；j1 被 runner=job 价格任务排除
	if len(items) != 5 {
		t.Fatalf("应五条（j1 排除）：%d %s", len(items), rec.Body.String())
	}
	order := []string{}
	for _, raw := range items {
		order = append(order, raw.(map[string]any)["run_id"].(string))
	}
	if strings.Join(order, ",") != "crawl:7,cycle:9,f1,j2,t1" {
		t.Fatalf("排序不符：%v", order)
	}
	first := items[0].(map[string]any)
	if first["session_id"] != "runtime" || first["runner"] != "task" {
		t.Fatal(first)
	}
	meta := first["meta"].(map[string]any)
	if meta["task"] != "price_repair" || meta["domainStatus"] != "done" || meta["cancellable"] != false {
		t.Fatalf("域行 meta 不符：%v", meta)
	}
	progress := meta["progress"].(map[string]any)
	if progress["done"] != float64(3) || progress["total"] != float64(9) || progress["success"] != float64(2) {
		t.Fatalf("progress 不符：%v", progress)
	}
	if first["error_code"] != "lastfail" {
		t.Fatalf("error_code 不符：%v", first["error_code"])
	}
	if first["created_at"] != "2026-11-14T12:00:00" {
		t.Fatalf("created_at 应 ISO 形态：%v", first["created_at"])
	}
	// 账本行投影：cancellable 按 runner=fake
	fake := items[2].(map[string]any)
	if fake["meta"].(map[string]any)["cancellable"] != true {
		t.Fatalf("fake 运行应可取消：%v", fake)
	}
	// status 过滤（域状态映射）
	rec = do("GET", "/api/v1/agent/runs?status=done", "")
	items = decodeRead(t, rec)["items"].([]any)
	if len(items) != 4 {
		t.Fatalf("done 过滤应四条：%s", rec.Body.String())
	}
	// limit 夹取
	rec = do("GET", "/api/v1/agent/runs?limit=2", "")
	items = decodeRead(t, rec)["items"].([]any)
	if len(items) != 2 {
		t.Fatalf("limit=2 应两条：%s", rec.Body.String())
	}
}

// TestAgentRunDetail404 缺席 404 逐字 + 显式坏域键不回落账本。
func TestAgentRunDetail404(t *testing.T) {
	db, _ := readFixtureDB(t)
	do := readMux(t, db, &Deps{})
	rec := do("GET", "/api/v1/agent/runs/absent", "")
	if rec.Code != 404 || !strings.Contains(rec.Body.String(), "run not found") {
		t.Fatalf("应 404 run not found：%d %s", rec.Code, rec.Body.String())
	}
	rec = do("GET", "/api/v1/agent/runs/cycle:0", "")
	if rec.Code != 404 {
		t.Fatalf("坏域键应 404：%d", rec.Code)
	}
}

// TestAgentRunEvents 分页（after_seq/limit）+ 域行空集 + 404。
func TestAgentRunEvents(t *testing.T) {
	db, _ := readFixtureDB(t)
	if _, err := db.Exec(`INSERT INTO agent_runs(id,session_id,runner,"trigger",meta,status,created_at)
		VALUES('r1','sess','fake','manual','{}','done','2026-11-14 09:00:00')`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO agent_events(run_id,seq,event_type,payload,created_at)
		VALUES('r1',1,'turn.received','{"a":1}','2026-11-14 09:00:00'),
		('r1',2,'run.state','{}','2026-11-14 09:01:00'),
		('r1',3,'run.finished','{}','2026-11-14 09:02:00')`); err != nil {
		t.Fatal(err)
	}
	do := readMux(t, db, &Deps{})
	rec := do("GET", "/api/v1/agent/runs/r1/events?after_seq=1&limit=1", "")
	if rec.Code != 200 {
		t.Fatalf("应 200：%d %s", rec.Code, rec.Body.String())
	}
	body := decodeRead(t, rec)
	if body["run_id"] != "r1" {
		t.Fatal(body)
	}
	items := body["items"].([]any)
	if len(items) != 1 || items[0].(map[string]any)["seq"] != float64(2) {
		t.Fatalf("after_seq 游标不符：%s", rec.Body.String())
	}
	rec = do("GET", "/api/v1/agent/runs/cycle:9/events", "")
	if rec.Code != 404 {
		t.Fatalf("缺席域行应 404：%d", rec.Code)
	}
	if _, err := db.Exec(`INSERT INTO price_cycles(id,kind,status,started_at) VALUES(9,'scheduled','completed','2026-11-14 10:00:00')`); err != nil {
		t.Fatal(err)
	}
	rec = do("GET", "/api/v1/agent/runs/cycle:9/events", "")
	body = decodeRead(t, rec)
	if list, ok := body["items"].([]any); !ok || len(list) != 0 {
		t.Fatalf("域行事件恒空集：%s", rec.Body.String())
	}
}

// TestAgentRunCancel 取消矩阵：缺席 404 带编号、终态 accepted=false、
// 域行非活 409、域行活体经 crawl stop、runner=job 409、fake 行取消跃迁写。
func TestAgentRunCancel(t *testing.T) {
	db, gate := readFixtureDB(t)
	seed := func(q string, args ...any) {
		t.Helper()
		if _, err := db.Exec(q, args...); err != nil {
			t.Fatal(err)
		}
	}
	seed(`INSERT INTO agent_runs(id,session_id,runner,"trigger",meta,status,created_at)
		VALUES('fake1','sess','fake','manual','{}','queued','2026-11-14 09:00:00'),
		('done1','sess','fake','manual','{}','done','2026-11-14 09:00:00'),
		('job1','sess','job','scheduled','{"job":"x"}','running','2026-11-14 09:00:00')`)
	seed(`INSERT INTO price_cycles(id,kind,status,started_at) VALUES(3,'scheduled','running','2026-11-14 10:00:00')`)
	seed(`INSERT INTO crawl_jobs(id,kind,status,cycle_id,stats_json,started_at) VALUES(7,'repair','running',NULL,'{"trigger":"manual","processed":1,"total":4}','2026-11-14 11:00:00')`)
	do := readMux(t, db, &Deps{WriteGate: gate})
	cancel := func(runID, body string) (int, map[string]any) {
		rec := do("POST", "/api/v1/agent/runs/"+runID+"/cancel", body)
		return rec.Code, decodeRead(t, rec)
	}
	status, body := cancel("absent", `{}`)
	if status != 404 || body["detail"] != "run not found: absent" {
		t.Fatalf("缺席应 404 带编号：%d %s", status, recBody(body))
	}
	status, body = cancel("done1", `{}`)
	if status != 200 || body["accepted"] != false || body["status"] != "done" {
		t.Fatalf("终态应幂等拒绝：%d %s", status, recBody(body))
	}
	status, body = cancel("cycle:999", `{}`)
	if status != 404 || body["detail"] != "run not found: cycle:999" {
		t.Fatalf("缺席域行应 404（Python get_run None 同径）：%d %s", status, recBody(body))
	}
	// 非终态且非活：planning 轮 5（活轮是 6，liveCycleID 取最大执行中 id）
	if _, err := db.Exec(`INSERT INTO price_cycles(id,kind,status,started_at) VALUES(5,'scheduled','planning','2026-11-14 10:00:00'),
		(6,'scheduled','running','2026-11-14 10:00:00')`); err != nil {
		t.Fatal(err)
	}
	status, body = cancel("cycle:5", `{}`)
	if status != 409 || body["detail"] != "run has no matching live execution" {
		t.Fatalf("在账非活轮应 409：%d %s", status, recBody(body))
	}
	if _, err := db.Exec(`INSERT INTO crawl_jobs(id,kind,status,cycle_id,stats_json,started_at) VALUES(8,'manual','pending',NULL,'{}','2026-11-14 10:00:00')`); err != nil {
		t.Fatal(err)
	}
	status, body = cancel("crawl:8", `{}`)
	if status != 409 {
		t.Fatalf("在账非活抓取任务应 409：%d %s", status, recBody(body))
	}
	// 活体域行：crawl:7 是 liveJobID（/crawl/active 桩返回 7）
	status, body = cancel("crawl:7", `{"reason": "user-stop"}`)
	if status != 200 || body["accepted"] != true {
		t.Fatalf("活体取消应受理：%d %s", status, recBody(body))
	}
	// runner=job：无执行句柄 409
	status, body = cancel("job1", `{}`)
	if status != 409 || body["detail"] != "run has no live stop handle" {
		t.Fatalf("runner=job 应 409：%d %s", status, recBody(body))
	}
	// fake 行：取消跃迁写三事件
	status, body = cancel("fake1", `{"reason": "user"}`)
	if status != 200 || body["accepted"] != true || body["status"] != "cancelled" {
		t.Fatalf("fake 取消应受理：%d %s", status, recBody(body))
	}
	var runStatus, finished string
	if err := db.QueryRow(`SELECT status, CAST(finished_at AS TEXT) FROM agent_runs WHERE id='fake1'`).Scan(&runStatus, &finished); err != nil {
		t.Fatal(err)
	}
	if runStatus != "cancelled" || finished == "" {
		t.Fatalf("取消跃迁未落库：%s %s", runStatus, finished)
	}
	var eventCount int
	if err := db.QueryRow(`SELECT COUNT(*) FROM agent_events WHERE run_id='fake1'`).Scan(&eventCount); err != nil {
		t.Fatal(err)
	}
	if eventCount != 3 {
		t.Fatalf("应三事件：%d", eventCount)
	}
	// 终态重复取消：幂等拒绝
	status, body = cancel("fake1", `{}`)
	if status != 200 || body["accepted"] != false || body["status"] != "cancelled" {
		t.Fatalf("重复取消应幂等：%d %s", status, recBody(body))
	}
	// reason 非串 422
	rec := do("POST", "/api/v1/agent/runs/absent/cancel", `{"reason": 5}`)
	if rec.Code != 422 {
		t.Fatalf("reason 非串应 422：%d", rec.Code)
	}
}

func recBody(body map[string]any) string {
	raw, _ := json.Marshal(body)
	return string(raw)
}
