package agent

// 批次三工具测试：每工具 1 正例 + 1 空态案（对照法 G4-6），另含
// _parse_ddg / steamSearchItems 纯函数单测、订阅行 URL 不泄漏断言、
// list_tasks 三源合并读与 cancel_task 取消形状案。
// 端点层用桩 mux（与真实路由同形状 JSON），DB 层用真实 SQLite 表；
// 出网工具走纯函数面（offline 不真出网）。

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"holdexar/server-go/internal/eventstream"
	"holdexar/server-go/internal/games"
	"holdexar/server-go/internal/store"
)

// opsEnv 批次三测试面：真实库 + 桩端点 mux + 注入依赖。
type opsEnv struct {
	mux  *http.ServeMux
	db   *sql.DB
	ring *eventstream.Ring
	deps *Deps

	// 桩状态（各端点返回值；nil 时用缺省形状）
	active         map[string]any
	cycles         []map[string]any
	events         []map[string]any
	jobs           []map[string]any
	latestFresh    map[string]any
	stopStopped    bool
	runStatus      int
	runBody        map[string]any
	healthRuns     []map[string]any
	jobRuns        map[string]any
	clash          map[string]any
	testSnap       map[string]any
	progressSnap   map[string]any
	subs           []map[string]any
	refreshStatus  int
	refreshBody    map[string]any
	readiness      map[string]any
	version        string
	updateProgress map[string]any
	updatePending  map[string]any
	smtp           map[string]any
	smtpTestStatus int
}

func newOpsEnv(t *testing.T) *opsEnv {
	t.Helper()
	dataDir := t.TempDir()
	db, err := store.Open(filepath.Join(dataDir, "holdexar.db"))
	if err != nil {
		t.Fatalf("打开测试库失败: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	for _, stmt := range []string{`
	CREATE TABLE games (appid INTEGER PRIMARY KEY, name TEXT, name_en TEXT, updated_at TEXT);
	CREATE TABLE game_current_prices (appid INTEGER, region_code TEXT, price_status TEXT,
		attempt_outcome TEXT, updated_at TEXT);
	CREATE TABLE crawl_regions (code TEXT PRIMARY KEY, name TEXT, currency TEXT,
		enabled INTEGER DEFAULT 1, sort INTEGER DEFAULT 0, updated_at TEXT);
	CREATE TABLE crawl_jobs (id INTEGER PRIMARY KEY, kind TEXT, status TEXT, cycle_id INTEGER,
		stats_json TEXT, started_at TEXT, finished_at TEXT);
	CREATE TABLE price_cycles (id INTEGER PRIMARY KEY, kind TEXT, status TEXT,
		batches_done INTEGER, batches_expected INTEGER, started_at TEXT, finished_at TEXT);
	CREATE TABLE agent_runs (id TEXT PRIMARY KEY, session_id TEXT, runner TEXT, "trigger" TEXT,
		meta JSON, status TEXT, error_code TEXT, created_at TEXT, started_at TEXT, finished_at TEXT);
	CREATE TABLE proxies (id INTEGER PRIMARY KEY, enabled INTEGER, status TEXT);
	CREATE TABLE proxy_subscriptions (id INTEGER PRIMARY KEY, kind TEXT, url TEXT, label TEXT,
		deprecated INTEGER DEFAULT 0);
	CREATE TABLE proxy_nodes (id INTEGER PRIMARY KEY, node_id TEXT UNIQUE, runtime_name TEXT,
		state TEXT, exit_ip TEXT, subscription_id INTEGER, last_l2_at TEXT);
	CREATE TABLE health_observations (id INTEGER PRIMARY KEY, node_id TEXT, level TEXT,
		latency_ms INTEGER, observed_at TEXT);
	`} {
		if _, err := db.Exec(stmt); err != nil {
			t.Fatalf("建表失败: %v", err)
		}
	}
	env := &opsEnv{mux: http.NewServeMux(), db: db}
	env.ring = eventstream.NewRing(2000)
	env.deps = &Deps{
		LogRing:  env.ring,
		WriteGate: store.NewWriteGate(db, filepath.Join(dataDir, "holdexar.db")),
		ResolveProxy: func(ctx context.Context) (string, bool) { return "", false },
		ImportAppids: func(ctx context.Context, appids []any) (map[string]any, error) {
			return games.ImportAppids(ctx, db, appids)
		},
		AppIDFreshness: func(ctx context.Context, appids []int64) ([]map[string]any, error) {
			out := []map[string]any{}
			for _, id := range appids {
				out = append(out, map[string]any{
					"appid": id, "freshness": "fresh", "observedAt": "2026-11-14T08:00:00",
				})
			}
			return out, nil
		},
	}
	registerOpsStubs(env)
	return env
}

// writeStubJSON 桩端点响应助手。
func writeStubJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if v == nil {
		_, _ = w.Write([]byte("null"))
		return
	}
	_ = json.NewEncoder(w).Encode(v)
}

// registerOpsStubs 挂载批次三工具消费的全部端点桩（形状对齐真实路由）。
func registerOpsStubs(env *opsEnv) {
	mux := env.mux
	mux.HandleFunc("GET /api/v1/crawl/active", func(w http.ResponseWriter, r *http.Request) {
		writeStubJSON(w, 200, env.active)
	})
	mux.HandleFunc("GET /api/v1/crawl/cycles", func(w http.ResponseWriter, r *http.Request) {
		writeStubJSON(w, 200, env.cycles)
	})
	mux.HandleFunc("GET /api/v1/crawl/price-events", func(w http.ResponseWriter, r *http.Request) {
		writeStubJSON(w, 200, env.events)
	})
	mux.HandleFunc("GET /api/v1/crawl/jobs", func(w http.ResponseWriter, r *http.Request) {
		writeStubJSON(w, 200, env.jobs)
	})
	mux.HandleFunc("GET /api/v1/crawl/freshness/latest", func(w http.ResponseWriter, r *http.Request) {
		writeStubJSON(w, 200, env.latestFresh)
	})
	mux.HandleFunc("POST /api/v1/crawl/stop", func(w http.ResponseWriter, r *http.Request) {
		writeStubJSON(w, 200, map[string]any{"stopped": env.stopStopped})
	})
	mux.HandleFunc("POST /api/v1/crawl/run", func(w http.ResponseWriter, r *http.Request) {
		status := env.runStatus
		if status == 0 {
			status = 200
		}
		writeStubJSON(w, status, env.runBody)
	})
	mux.HandleFunc("GET /api/v1/proxypool/health-runs", func(w http.ResponseWriter, r *http.Request) {
		writeStubJSON(w, 200, map[string]any{"runs": env.healthRuns})
	})
	mux.HandleFunc("GET /api/v1/proxypool/job-runs", func(w http.ResponseWriter, r *http.Request) {
		writeStubJSON(w, 200, env.jobRuns)
	})
	mux.HandleFunc("GET /api/v1/proxies/clash", func(w http.ResponseWriter, r *http.Request) {
		writeStubJSON(w, 200, env.clash)
	})
	mux.HandleFunc("POST /api/v1/proxies/clash/test", func(w http.ResponseWriter, r *http.Request) {
		writeStubJSON(w, 200, env.testSnap)
	})
	mux.HandleFunc("GET /api/v1/proxies/clash/test/progress", func(w http.ResponseWriter, r *http.Request) {
		writeStubJSON(w, 200, env.progressSnap)
	})
	mux.HandleFunc("GET /api/v1/proxies/subscriptions", func(w http.ResponseWriter, r *http.Request) {
		writeStubJSON(w, 200, map[string]any{"items": env.subs})
	})
	mux.HandleFunc("POST /api/v1/proxies/subscriptions/{id}/refresh", func(w http.ResponseWriter, r *http.Request) {
		status := env.refreshStatus
		if status == 0 {
			status = 200
		}
		writeStubJSON(w, status, env.refreshBody)
	})
	mux.HandleFunc("GET /api/v1/system/readiness", func(w http.ResponseWriter, r *http.Request) {
		writeStubJSON(w, 200, env.readiness)
	})
	mux.HandleFunc("GET /api/v1/info", func(w http.ResponseWriter, r *http.Request) {
		writeStubJSON(w, 200, map[string]any{"version": env.version})
	})
	mux.HandleFunc("GET /api/v1/system/update-progress", func(w http.ResponseWriter, r *http.Request) {
		writeStubJSON(w, 200, env.updateProgress)
	})
	mux.HandleFunc("GET /api/v1/system/update-pending", func(w http.ResponseWriter, r *http.Request) {
		writeStubJSON(w, 200, env.updatePending)
	})
	mux.HandleFunc("GET /api/v1/alerts/smtp", func(w http.ResponseWriter, r *http.Request) {
		writeStubJSON(w, 200, env.smtp)
	})
	mux.HandleFunc("POST /api/v1/alerts/smtp/test", func(w http.ResponseWriter, r *http.Request) {
		status := env.smtpTestStatus
		if status == 0 {
			status = 200
		}
		writeStubJSON(w, status, map[string]any{"ok": status == 200})
	})
}

// opsExecute 批次三工具快捷执行入口（自带注册表与 deps 注入）。
func opsExecute(t *testing.T, env *opsEnv, name string, argsJSON string) map[string]any {
	t.Helper()
	reg := newRegistry()
	if err := RegisterOpsRuntimeTools(reg); err != nil {
		t.Fatalf("批次三注册失败: %v", err)
	}
	spec := reg.get(name)
	if spec == nil {
		t.Fatalf("工具未注册: %s", name)
	}
	var args map[string]any
	if argsJSON != "" {
		dec := jsonDecoder([]byte(argsJSON))
		if err := dec.Decode(&args); err != nil {
			t.Fatalf("入参解码失败: %v", err)
		}
	}
	res := Execute(&CallContext{
		Ctx: context.Background(), Args: args,
		fetch: newMuxFetcher(env.mux), post: newMuxPoster(env.mux),
		db: env.db, deps: env.deps,
	}, spec, false)
	var payload map[string]any
	if err := json.Unmarshal([]byte(encodeJSON(payloadWithStatus(res))), &payload); err != nil {
		t.Fatalf("结果解码失败: %v", err)
	}
	return payload
}

// rowMap 行卡第 i 行（map 视图）。
func rowMap(t *testing.T, payload map[string]any, i int) map[string]any {
	t.Helper()
	rows, ok := payload["rows"].([]any)
	if !ok || len(rows) <= i {
		t.Fatalf("行卡无第 %d 行: %v", i, payload)
	}
	row, ok := rows[i].(map[string]any)
	if !ok {
		t.Fatalf("行 %d 形状不对: %v", i, rows[i])
	}
	return row
}

// ─── 注册表 ─────────────────────────────────────────────────────────────────

func TestOpsRuntimeRegistry25(t *testing.T) {
	reg := newRegistry()
	if err := RegisterOpsRuntimeTools(reg); err != nil {
		t.Fatal(err)
	}
	want := []string{
		"crawl_active", "price_freshness", "recent_cycles", "price_events", "crawl_stop",
		"recent_job_failures", "write_gate_status", "read_logs", "system_diagnostics",
		"subscriptions_list", "subscription_sync", "pool_health_history", "pool_job_runs",
		"proxy_pool_status", "proxy_exit_detail", "proxy_latency_test", "sync_subscriptions",
		"web_search", "refresh_game_price", "notify_test", "update_status", "list_tasks",
		"cancel_task", "search_steam", "ingest_appids",
	}
	if len(reg.all()) != len(want) {
		t.Fatalf("注册数=%d 期望=%d", len(reg.all()), len(want))
	}
	for _, name := range want {
		spec := reg.get(name)
		if spec == nil {
			t.Fatalf("缺工具: %s", name)
		}
		if spec.Group != GroupRead {
			t.Fatalf("%s 组=%s 非 read", name, spec.Group)
		}
		if spec.TimeoutS != budgetFor(name) {
			t.Fatalf("%s 预算=%v 期望=%v", name, spec.TimeoutS, budgetFor(name))
		}
	}
	// 预算分档抽查（批次三清单）。
	for name, want := range map[string]float64{
		"crawl_stop": 60, "cancel_task": 60, "list_tasks": 60,
		"proxy_latency_test": 180, "web_search": 40, "search_steam": 40,
		"sync_subscriptions": 240, "subscription_sync": 240,
		"crawl_active": 20,
	} {
		if got := budgetFor(name); got != want {
			t.Fatalf("预算 %s=%v 期望 %v", name, got, want)
		}
	}
}

// ─── 爬取运维读面 ───────────────────────────────────────────────────────────

func TestCrawlActiveShape(t *testing.T) {
	env := newOpsEnv(t)
	env.active = map[string]any{
		"activeJobId": 7, "busy": true, "maintenance": true,
		"maintenanceInfo": map[string]any{"kind": "health", "reason": "体检"},
		"throttled":       3,
	}
	payload := opsExecute(t, env, "crawl_active", "")
	if payload["titleKey"] != "crawlActive" {
		t.Fatalf("titleKey=%v", payload["titleKey"])
	}
	if rowMap(t, payload, 0)["v"] != "是" || rowMap(t, payload, 0)["tone"] != "ok" {
		t.Fatalf("行0=%v", rowMap(t, payload, 0))
	}
	if rowMap(t, payload, 1)["v"] != "7" {
		t.Fatalf("活动任务行=%v", rowMap(t, payload, 1))
	}
	if rowMap(t, payload, 2)["v"] != "health" || rowMap(t, payload, 3)["v"] != "3" {
		t.Fatalf("体检/限流行=%v %v", rowMap(t, payload, 2), rowMap(t, payload, 3))
	}
	// 空态：闲置 → 仅两行、无体检/限流。
	env.active = map[string]any{"activeJobId": nil, "busy": false, "maintenance": false, "throttled": 0}
	payload = opsExecute(t, env, "crawl_active", "")
	if len(payload["rows"].([]any)) != 2 || rowMap(t, payload, 0)["v"] != "否" {
		t.Fatalf("空态=%v", payload["rows"])
	}
}

func TestPriceFreshnessShape(t *testing.T) {
	env := newOpsEnv(t)
	env.latestFresh = map[string]any{"lastSuccessAt": "2026-11-14T08:00:00", "serverNow": "2026-11-14T09:00:00"}
	payload := opsExecute(t, env, "price_freshness", `{"appids": [900001, 900002, 900001]}`)
	if payload["total"] != float64(2) || payload["lastSuccessAt"] != "2026-11-14T08:00:00" {
		t.Fatalf("payload=%v", payload)
	}
	if rowMap(t, payload, 0)["k"] != "900001" || rowMap(t, payload, 0)["v"] != "fresh" {
		t.Fatalf("行0=%v", rowMap(t, payload, 0))
	}
	// 空态 1：无有效对象 → no_appids。
	payload = opsExecute(t, env, "price_freshness", `{"appids": []}`)
	if payload["note"] != "no_appids" {
		t.Fatalf("空态=%v", payload)
	}
	// 空态 2：依赖未装配 → not_available（failed 归一）。
	env.deps.AppIDFreshness = nil
	payload = opsExecute(t, env, "price_freshness", `{"appids": [1]}`)
	if payload["status"] != "error" || payload["note"] != "not_available" {
		t.Fatalf("不可用=%v", payload)
	}
}

func TestRecentCyclesShape(t *testing.T) {
	env := newOpsEnv(t)
	env.cycles = []map[string]any{
		{"id": 5, "status": "completed", "batchesDone": 3, "batchesExpected": 5,
			"startedAt": "2026-11-14T06:00:00", "finishedAt": "2026-11-14T06:30:00"},
		{"id": 4, "status": "failed", "batchesDone": nil, "batchesExpected": 5,
			"startedAt": "2026-11-14T00:00:00"},
	}
	payload := opsExecute(t, env, "recent_cycles", `{"limit": 2}`)
	if payload["total"] != float64(2) {
		t.Fatalf("total=%v", payload["total"])
	}
	r0 := rowMap(t, payload, 0)
	if r0["k"] != "#5" || r0["v"] != "completed" || r0["v2"] != "3/5" || r0["at"] != "2026-11-14T06:30:00" {
		t.Fatalf("行0=%v", r0)
	}
	r1 := rowMap(t, payload, 1)
	if r1["v2"] != "None/5" || r1["at"] != "2026-11-14T00:00:00" {
		t.Fatalf("行1=%v", r1)
	}
	// 空态：无轮次 → 空行卡（rows 不带行）。
	env.cycles = nil
	payload = opsExecute(t, env, "recent_cycles", "")
	if rows, ok := payload["rows"].([]any); !ok || len(rows) != 0 || payload["total"] != float64(0) {
		t.Fatalf("空态=%v", payload)
	}
}

func TestPriceEventsShape(t *testing.T) {
	env := newOpsEnv(t)
	env.events = []map[string]any{
		{"eventType": "NEW_HISTORICAL_LOW", "appid": 900001, "region": "cn",
			"occurredAt": "2026-11-14T07:00:00"},
	}
	payload := opsExecute(t, env, "price_events", "")
	if payload["total"] != float64(1) {
		t.Fatalf("total=%v", payload["total"])
	}
	r0 := rowMap(t, payload, 0)
	if r0["k"] != "NEW_HISTORICAL_LOW·900001" || r0["v"] != "cn" {
		t.Fatalf("行0=%v", r0)
	}
	// 空态 1：非法事件类型 → bad_event_type。
	payload = opsExecute(t, env, "price_events", `{"event_type": "not_a_type"}`)
	if payload["note"] != "bad_event_type" {
		t.Fatalf("bad_type=%v", payload)
	}
	// 空态 2：无事件 → 空行卡。
	env.events = nil
	payload = opsExecute(t, env, "price_events", "")
	if rows, ok := payload["rows"].([]any); !ok || len(rows) != 0 {
		t.Fatalf("空态=%v", payload)
	}
}

func TestCrawlStopShape(t *testing.T) {
	env := newOpsEnv(t)
	env.stopStopped = true
	payload := opsExecute(t, env, "crawl_stop", "")
	if rowMap(t, payload, 0)["v"] != "已受理" || rowMap(t, payload, 0)["tone"] != "ok" {
		t.Fatalf("正例=%v", payload)
	}
	env.stopStopped = false
	payload = opsExecute(t, env, "crawl_stop", "")
	if rowMap(t, payload, 0)["v"] != "当前无任务" || rowMap(t, payload, 0)["tone"] != "warn" {
		t.Fatalf("空态=%v", payload)
	}
}

func TestRecentJobFailuresShape(t *testing.T) {
	env := newOpsEnv(t)
	env.jobs = []map[string]any{
		{"id": 12, "kind": "missing", "status": "failed", "error": "代理不可用",
			"startedAt": "2026-11-14T07:00:00"},
		{"id": 11, "kind": "manual", "status": "done"},
	}
	payload := opsExecute(t, env, "recent_job_failures", "")
	if payload["total"] != float64(1) {
		t.Fatalf("total=%v", payload["total"])
	}
	r0 := rowMap(t, payload, 0)
	if r0["k"] != "#12 missing" || r0["v"] != "代理不可用" || r0["tone"] != "bad" {
		t.Fatalf("行0=%v", r0)
	}
	// 空态：无失败任务 → 空行卡。
	env.jobs = nil
	payload = opsExecute(t, env, "recent_job_failures", "")
	if payload["total"] != float64(0) {
		t.Fatalf("空态=%v", payload)
	}
}

// ─── 写闸 / 日志 / 诊断 ─────────────────────────────────────────────────────

func TestWriteGateStatusShape(t *testing.T) {
	env := newOpsEnv(t)
	payload := opsExecute(t, env, "write_gate_status", "")
	rows := payload["rows"].([]any)
	if len(rows) != 2 {
		t.Fatalf("行数=%v", payload["rows"])
	}
	r0, r1 := rowMap(t, payload, 0), rowMap(t, payload, 1)
	if r0["vKey"] != "gateIdle" || r0["tone"] != "ok" || r0["v"] != "" {
		t.Fatalf("行0=%v", r0)
	}
	if r1["vKey"] != "gateWaiting" || r1["tone"] != "ok" {
		t.Fatalf("行1=%v", r1)
	}
	// 空态：依赖未装配 → not_available。
	env.deps.WriteGate = nil
	payload = opsExecute(t, env, "write_gate_status", "")
	if payload["status"] != "error" || payload["note"] != "not_available" {
		t.Fatalf("不可用=%v", payload)
	}
}

func TestReadLogsShape(t *testing.T) {
	env := newOpsEnv(t)
	env.ring.Write("10:00:00 [信息] 服务端 · 抓取批次已开始 │ 款数=3")
	env.ring.Write("10:00:01 [注意] 服务端 · 限流让路等待下一拍")
	env.ring.Write("10:00:02 [失败] 服务端 · 抓取批次未完成 │ 款数=3")
	env.ring.Write("10:00:03 [信息] 服务端 · 无标签词之外的行")
	// 正例：级别过滤 error + 关键词。
	payload := opsExecute(t, env, "read_logs", `{"level": "error", "keyword": "批次"}`)
	if payload["total"] != float64(1) {
		t.Fatalf("total=%v", payload["total"])
	}
	r0 := rowMap(t, payload, 0)
	if r0["tone"] != "bad" || r0["v"] != "抓取批次未完成 │ 款数=3" {
		t.Fatalf("行0=%v", r0)
	}
	if !strings.HasPrefix(r0["k"].(string), "10:00:02") {
		t.Fatalf("行首=%v", r0["k"])
	}
	// 空态：关键词零命中 → 空行卡无 counts。
	payload = opsExecute(t, env, "read_logs", `{"keyword": "不存在词"}`)
	if rows, ok := payload["rows"].([]any); !ok || len(rows) != 0 {
		t.Fatalf("空态=%v", payload)
	}
	if _, has := payload["counts"]; has {
		t.Fatalf("空态不应带 counts: %v", payload["counts"])
	}
	// 不可用：Ring 未注入。
	env.deps.LogRing = nil
	payload = opsExecute(t, env, "read_logs", "")
	if payload["status"] != "error" || payload["note"] != "not_available" {
		t.Fatalf("不可用=%v", payload)
	}
}

func TestSystemDiagnosticsShape(t *testing.T) {
	oldTakenOver := networkTakenOver
	networkTakenOver = func(ctx context.Context) bool { return false }
	t.Cleanup(func() { networkTakenOver = oldTakenOver })

	env := newOpsEnv(t)
	env.version = "1.2.3"
	env.readiness = map[string]any{"strategy": "proxy_first", "httpReady": true}
	env.clash = map[string]any{"running": false}
	env.cycles = []map[string]any{{
		"id": 9, "status": "failed", "error": "代理池不可用",
		"batchesDone": 1, "batchesExpected": 5, "startedAt": "2026-11-14T06:00:00",
	}}
	env.healthRuns = nil
	env.jobRuns = map[string]any{
		"summary": map[string]any{"runs": 2, "success": 1, "partial": 0, "failed": 1},
		"items": []map[string]any{
			{"id": 3, "status": "failed", "kind": "auto", "startedAt": "2026-11-14T07:00:00",
				"taskCount": 2, "successCount": 0, "node": "lane-1"},
		},
	}
	if _, err := env.db.Exec(`INSERT INTO game_current_prices (appid, region_code, price_status, updated_at)
		VALUES (1, 'cn', 'missing', '2026-01-01 00:00:00')`); err != nil {
		t.Fatal(err)
	}
	env.ring.Write("10:00:00 [成功] 服务端 · 写库分段完成 │ 款数=100 总计=1200毫秒 等写锁=45000毫秒")
	payload := opsExecute(t, env, "system_diagnostics", "")
	if payload["titleKey"] != "diag" {
		t.Fatalf("titleKey=%v", payload["titleKey"])
	}
	r0 := rowMap(t, payload, 0)
	if r0["k"] != "v1.2.3" || r0["v"] != "策略 proxy_first · 出口 0" {
		t.Fatalf("身份行=%v", r0)
	}
	if rowMap(t, payload, 1)["tone"] != "bad" {
		t.Fatalf("价格轮行=%v", rowMap(t, payload, 1))
	}
	// 信号序：last_cycle_failed → write_lock_wait_high → job_runs_failed → missing_backlog → proxy_pool_unavailable。
	codes := []string{}
	for _, raw := range payload["rows"].([]any)[2:] {
		codes = append(codes, raw.(map[string]any)["k"].(string))
	}
	want := []string{"last_cycle_failed", "write_lock_wait_high", "job_runs_failed", "missing_backlog", "proxy_pool_unavailable"}
	if len(codes) != len(want) {
		t.Fatalf("信号=%v", codes)
	}
	for i := range want {
		if codes[i] != want[i] {
			t.Fatalf("信号序=%v", codes)
		}
	}
	// 空态：无异常 → 单行「诊断面未检出异常信号」。
	env2 := newOpsEnv(t)
	env2.version = "1.0.0"
	env2.version = "1.0.0"
	env2.readiness = map[string]any{"strategy": "direct_only"}
	env2.clash = map[string]any{"running": false}
	env2.cycles = nil
	env2.jobRuns = nil
	payload = opsExecute(t, env2, "system_diagnostics", "")
	rows := payload["rows"].([]any)
	if len(rows) != 2 || rowMap(t, payload, 1)["v"] != "诊断面未检出异常信号" {
		t.Fatalf("空态=%v", payload["rows"])
	}
}

// ─── 代理面 ─────────────────────────────────────────────────────────────────

const secretSubURL = "https://panel.example.com/api/v1/secret/token=abc123/path"

func subStub(id int64, label string, deprecated bool) map[string]any {
	return map[string]any{
		"id": id, "kind": "clash", "url": secretSubURL, "label": label,
		"deprecated": deprecated, "lastImportedAt": "2026-11-14T07:30:00",
		"lastStats": map[string]any{"alive": 4, "total": 10},
	}
}

func TestSubscriptionsListShape(t *testing.T) {
	env := newOpsEnv(t)
	env.subs = []map[string]any{subStub(7, "机场A", false), subStub(8, "", true)}
	payload := opsExecute(t, env, "subscriptions_list", "")
	if payload["total"] != float64(2) {
		t.Fatalf("total=%v", payload["total"])
	}
	r0, r1 := rowMap(t, payload, 0), rowMap(t, payload, 1)
	if r0["k"] != "机场A" || r0["tone"] != "ok" {
		t.Fatalf("行0=%v", r0)
	}
	if r0["v"] != "https://panel.example.com · 4/10 · 2026-11-14 07:30" {
		t.Fatalf("订阅行=%v", r0["v"])
	}
	if r1["k"] != "#8" || r1["tone"] != "bad" {
		t.Fatalf("行1=%v", r1)
	}
	// 红线：订阅行绝不泄漏 URL（token/路径不出工具面）。
	for _, key := range []string{"rows"} {
		if strings.Contains(encodeJSON(payload[key]), "token=abc123") ||
			strings.Contains(encodeJSON(payload[key]), "/api/v1/secret") {
			t.Fatalf("订阅行泄漏 URL: %v", payload[key])
		}
	}
	// 空态：无订阅 → subs_none。
	env.subs = nil
	payload = opsExecute(t, env, "subscriptions_list", "")
	if payload["note"] != "subs_none" {
		t.Fatalf("空态=%v", payload)
	}
}

func TestSubscriptionSyncShape(t *testing.T) {
	env := newOpsEnv(t)
	env.subs = []map[string]any{subStub(7, "机场A", false)}
	env.refreshBody = map[string]any{"id": 7, "nodes": 12, "alive": 8, "total": 10}
	payload := opsExecute(t, env, "subscription_sync", `{"sub_id": 7}`)
	r0 := rowMap(t, payload, 0)
	if r0["k"] != "机场A" || r0["vKey"] != "subsSynced" || r0["tone"] != "ok" {
		t.Fatalf("正例=%v", r0)
	}
	if data, ok := r0["data"].(map[string]any); !ok || data["n"] != float64(12) {
		t.Fatalf("data=%v", r0["data"])
	}
	// 空态 1：订阅不存在 → sub_missing。
	payload = opsExecute(t, env, "subscription_sync", `{"sub_id": 99}`)
	if payload["note"] != "sub_missing" {
		t.Fatalf("缺订阅=%v", payload)
	}
	// 空态 2：刷新失败 → kind=failed + error 文案。
	env.refreshStatus = 502
	env.refreshBody = map[string]any{"detail": "本次订阅下载未完成"}
	payload = opsExecute(t, env, "subscription_sync", `{"sub_id": 7}`)
	if payload["kind"] != "failed" || payload["error"] != "本次订阅下载未完成" {
		t.Fatalf("失败=%v", payload)
	}
}

func TestPoolHealthHistoryShape(t *testing.T) {
	env := newOpsEnv(t)
	env.healthRuns = []map[string]any{
		{"id": 3, "channel": "steam", "startedAt": "2026-11-14T07:00:00", "total": 10,
			"steamOk": 8, "failed": 0, "endReason": nil},
		{"id": 2, "channel": "steam", "startedAt": "2026-11-13T07:00:00", "total": 10,
			"steamOk": 5, "failed": 2, "endReason": "超时收尾"},
	}
	payload := opsExecute(t, env, "pool_health_history", "")
	if payload["total"] != float64(2) {
		t.Fatalf("total=%v", payload["total"])
	}
	r0 := rowMap(t, payload, 0)
	if r0["k"] != "11-14 07:00" || r0["v"] != "覆盖 10 · 可用 8 · steam" || r0["tone"] != "ok" {
		t.Fatalf("行0=%v", r0)
	}
	if rowMap(t, payload, 1)["tone"] != "bad" {
		t.Fatalf("失败轮行=%v", rowMap(t, payload, 1))
	}
	// 空态：无体检台账 → no_health_runs。
	env.healthRuns = nil
	payload = opsExecute(t, env, "pool_health_history", "")
	if payload["note"] != "no_health_runs" {
		t.Fatalf("空态=%v", payload)
	}
}

func TestPoolJobRunsShape(t *testing.T) {
	env := newOpsEnv(t)
	env.jobRuns = map[string]any{
		"summary": map[string]any{"runs": 3, "success": 2, "partial": 0, "failed": 1},
		"items": []map[string]any{
			{"id": 21, "status": "running", "kind": "auto", "startedAt": "2026-11-14T07:00:00",
				"taskCount": 2, "successCount": 1, "node": "lane-1"},
		},
	}
	payload := opsExecute(t, env, "pool_job_runs", "")
	if payload["total"] != float64(1) {
		t.Fatalf("total=%v", payload["total"])
	}
	r0 := rowMap(t, payload, 0)
	if r0["k"] != "今日" || r0["v"] != "3 轮 · 成功 2 · 失败 1" || r0["tone"] != "bad" {
		t.Fatalf("今日行=%v", r0)
	}
	r1 := rowMap(t, payload, 1)
	if r1["k"] != "#21 2026-11-14 07:00" || r1["v"] != "1/2 · auto · lane-1" || r1["tone"] != "warn" {
		t.Fatalf("作业行=%v", r1)
	}
	// 空态：无作业 → no_job_runs。
	env.jobRuns = map[string]any{"summary": nil, "items": nil}
	payload = opsExecute(t, env, "pool_job_runs", "")
	if payload["note"] != "no_job_runs" {
		t.Fatalf("空态=%v", payload)
	}
}

func TestProxyPoolStatusShape(t *testing.T) {
	env := newOpsEnv(t)
	if _, err := env.db.Exec(`INSERT INTO proxies (id, enabled, status) VALUES (1, 1, 'ok'), (2, 0, 'ok')`); err != nil {
		t.Fatal(err)
	}
	if _, err := env.db.Exec(`INSERT INTO proxy_subscriptions (id, kind, url, label, deprecated) VALUES (7, 'clash', 'u', 'A', 0)`); err != nil {
		t.Fatal(err)
	}
	if _, err := env.db.Exec(`INSERT INTO proxy_nodes (id, node_id, runtime_name, state, exit_ip, subscription_id, last_l2_at) VALUES
		(1, 'n1', '节点1', 'ACTIVE', '1.2.3.4', 7, '2026-11-14 07:00:00'),
		(2, 'n2', '节点2', 'ACTIVE', '1.2.3.4', 7, '2026-11-14 07:00:00'),
		(3, 'n3', '节点3', 'DEAD', '5.6.7.8', 7, NULL)`); err != nil {
		t.Fatal(err)
	}
	env.clash = map[string]any{"running": true, "selectedSubscriptionId": 7}
	payload := opsExecute(t, env, "proxy_pool_status", "")
	rows := payload["rows"].([]any)
	poolData := rows[0].(map[string]any)["data"].(map[string]any)
	exitData := rows[1].(map[string]any)["data"].(map[string]any)
	if poolData["ok"] != float64(1) || poolData["total"] != float64(2) {
		t.Fatalf("池行=%v", poolData)
	}
	// 出口去重：n1/n2 同出口 IP → 1 个可用出口；n3 DEAD 不算。
	if exitData["ok"] != float64(1) || exitData["total"] != float64(2) {
		t.Fatalf("出口行=%v", exitData)
	}
	if rows[2].(map[string]any)["vKey"] != "proxyRunning" {
		t.Fatalf("运行行=%v", rows[2])
	}
	// 空态：内核未跑 → proxyStopped。
	env.clash = map[string]any{"running": false}
	payload = opsExecute(t, env, "proxy_pool_status", "")
	rows = payload["rows"].([]any)
	if rows[2].(map[string]any)["vKey"] != "proxyStopped" {
		t.Fatalf("停跑态=%v", rows[2])
	}
	if rows[1].(map[string]any)["data"].(map[string]any)["ok"] != float64(0) {
		t.Fatalf("停跑出口=%v", rows[1])
	}
}

func TestProxyExitDetailShape(t *testing.T) {
	env := newOpsEnv(t)
	if _, err := env.db.Exec(`INSERT INTO proxy_subscriptions (id, kind, url, label, deprecated) VALUES (7, 'clash', 'u', 'A', 0)`); err != nil {
		t.Fatal(err)
	}
	if _, err := env.db.Exec(`INSERT INTO proxy_nodes (id, node_id, runtime_name, state, exit_ip, subscription_id) VALUES
		(1, 'n1', '慢节点', 'ACTIVE', '1.2.3.4', 7),
		(2, 'n2', '快节点', 'STALE', '5.6.7.8', 7),
		(3, 'n3', '死节点', 'DEAD', '', 7)`); err != nil {
		t.Fatal(err)
	}
	if _, err := env.db.Exec(`INSERT INTO health_observations (id, node_id, level, latency_ms, observed_at) VALUES
		(1, 'n1', 'L2', 300, '2026-11-14 07:00:00'),
		(2, 'n2', 'L2', 120, '2026-11-14 07:00:00')`); err != nil {
		t.Fatal(err)
	}
	env.clash = map[string]any{"running": true, "selectedSubscriptionId": 7}
	payload := opsExecute(t, env, "proxy_exit_detail", "")
	if payload["total"] != float64(3) {
		t.Fatalf("total=%v", payload["total"])
	}
	r0, r1 := rowMap(t, payload, 0), rowMap(t, payload, 1)
	// 存活按延迟升序在前（快节点 120ms < 慢节点 300ms）。
	if r0["k"] != "快节点" || r0["v"] != "5.6.7.8 · 120ms" || r0["tone"] != "ok" {
		t.Fatalf("行0=%v", r0)
	}
	if r1["v"] != "1.2.3.4 · 300ms" {
		t.Fatalf("行1=%v", r1)
	}
	if rowMap(t, payload, 2)["tone"] != "bad" {
		t.Fatalf("死节点=%v", rowMap(t, payload, 2))
	}
	// 空态：未运行 → proxyStopped 单行。
	env.clash = map[string]any{"running": false}
	payload = opsExecute(t, env, "proxy_exit_detail", "")
	if rowMap(t, payload, 0)["vKey"] != "proxyStopped" {
		t.Fatalf("空态=%v", payload)
	}
}

func TestProxyLatencyTestShape(t *testing.T) {
	oldWait, oldPoll := proxyTestWaitS, proxyTestPollS
	proxyTestWaitS, proxyTestPollS = 2.0, 0.02
	t.Cleanup(func() { proxyTestWaitS, proxyTestPollS = oldWait, oldPoll })

	env := newOpsEnv(t)
	env.testSnap = map[string]any{"phase": "queued"}
	env.progressSnap = map[string]any{"phase": "done", "nodes": []map[string]any{
		{"name": "慢节点", "alive": true, "probed": true, "ms": 300, "exitIp": "1.2.3.4"},
		{"name": "快节点", "alive": true, "probed": true, "ms": 80, "exitIp": "5.6.7.8"},
		{"name": "死节点", "alive": false, "probed": true, "ms": nil, "exitIp": ""},
	}}
	payload := opsExecute(t, env, "proxy_latency_test", "")
	if payload["total"] != float64(3) {
		t.Fatalf("total=%v", payload["total"])
	}
	r0 := rowMap(t, payload, 0)
	if r0["v"] != "2/3" || r0["tone"] != "ok" {
		t.Fatalf("首行=%v", r0)
	}
	// 存活按延迟升序在前。
	if rowMap(t, payload, 1)["k"] != "快节点" || rowMap(t, payload, 1)["v"] != "5.6.7.8 · 80ms" {
		t.Fatalf("行1=%v", rowMap(t, payload, 1))
	}
	if rowMap(t, payload, 3)["v"] != "" || rowMap(t, payload, 3)["tone"] != "bad" {
		t.Fatalf("死节点=%v", rowMap(t, payload, 3))
	}
	// 空态 1：超窗未收口 → proxyTestPending。
	env.progressSnap = map[string]any{"phase": "running"}
	payload = opsExecute(t, env, "proxy_latency_test", "")
	if rowMap(t, payload, 0)["vKey"] != "proxyTestPending" {
		t.Fatalf("pending=%v", payload)
	}
	// 空态 2：检测失败 → error 行。
	env.progressSnap = map[string]any{"phase": "failed", "error": "内核未启动"}
	payload = opsExecute(t, env, "proxy_latency_test", "")
	if rowMap(t, payload, 0)["v"] != "内核未启动" || rowMap(t, payload, 0)["tone"] != "bad" {
		t.Fatalf("failed=%v", payload)
	}
}

func TestSyncSubscriptionsShape(t *testing.T) {
	env := newOpsEnv(t)
	env.subs = []map[string]any{subStub(7, "机场A", false), subStub(8, "退役B", true)}
	env.refreshBody = map[string]any{"id": 7, "nodes": 9}
	payload := opsExecute(t, env, "sync_subscriptions", "")
	if payload["total"] != float64(1) {
		t.Fatalf("total=%v（应跳过 deprecated）", payload["total"])
	}
	r0 := rowMap(t, payload, 0)
	if r0["k"] != "机场A" || r0["vKey"] != "subsSynced" || r0["tone"] != "ok" {
		t.Fatalf("行0=%v", r0)
	}
	// 红线：同步回执绝不含订阅链接。
	if strings.Contains(encodeJSON(payload), "token=abc123") {
		t.Fatalf("同步回执泄漏订阅 URL: %v", payload)
	}
	// 空态：全部退役 → subsNone。
	env.subs = []map[string]any{subStub(8, "退役B", true)}
	payload = opsExecute(t, env, "sync_subscriptions", "")
	if rowMap(t, payload, 0)["vKey"] != "subsNone" || rowMap(t, payload, 0)["tone"] != "warn" {
		t.Fatalf("空态=%v", payload)
	}
}

// ─── 网络出站 ───────────────────────────────────────────────────────────────

func TestParseDDG(t *testing.T) {
	html := `<div class="result">
		<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2F%25E5%2595%2586%25E5%2593%2581&amp;rut=xyz"><b>商品</b>页</a>
		<a class="result__snippet" href="#">这是 <i>摘要</i> 内容</a>
	</div>
	<a class="result__a" href="https://direct.example.org/a">直达</a>
	<a class="result__snippet" href="#">直达摘要</a>
	<a class="result__a" href="https://empty.example.org/b"></a>
	<a class="result__snippet" href="#">空标题</a>`
	hits := parseDDG(html, 5)
	if len(hits) != 2 {
		t.Fatalf("命中=%v", hits)
	}
	// uddg 反解（含 &amp; 还原与双重解码）+ 去标签。
	if hits[0].url != "https://example.com/商品" {
		t.Fatalf("uddg=%q", hits[0].url)
	}
	if hits[0].title != "商品页" || hits[0].snippet != "这是 摘要 内容" {
		t.Fatalf("解析=%+v", hits[0])
	}
	if hits[1].url != "https://direct.example.org/a" || hits[1].title != "直达" {
		t.Fatalf("直达=%+v", hits[1])
	}
	// 空标题跳过 + limit 上限。
	hits = parseDDG(html, 1)
	if len(hits) != 1 {
		t.Fatalf("limit=%v", hits)
	}
	if hits := parseDDG("", 5); len(hits) != 0 {
		t.Fatalf("空 HTML=%v", hits)
	}
}

func TestWebSearchShape(t *testing.T) {
	env := newOpsEnv(t)
	// 空态 1：空词 → bad_query。
	payload := opsExecute(t, env, "web_search", `{"query": "   "}`)
	if payload["note"] != "bad_query" {
		t.Fatalf("bad_query=%v", payload)
	}
	// 空态 2：出网不可达（代理指到拒绝端口，任何网络环境都确定性失败）→ 空行卡。
	env.deps.ResolveProxy = func(ctx context.Context) (string, bool) { return "http://127.0.0.1:1", true }
	payload = opsExecute(t, env, "web_search", `{"query": "holdexar"}`)
	if rows, ok := payload["rows"].([]any); !ok || len(rows) != 0 || payload["total"] != float64(0) {
		t.Fatalf("离线空态=%v", payload)
	}
}

func TestSteamSearchItems(t *testing.T) {
	data := map[string]any{"items": []any{
		map[string]any{"id": json.Number("900001"), "name": "Game A",
			"price": map[string]any{"currency": "USD", "final": json.Number("999"), "initial": json.Number("1999")}},
		map[string]any{"id": json.Number("900002"), "name": "Game B",
			"price": map[string]any{"currency": "CNY", "final": json.Number("4800"), "initial": json.Number("9600")}},
		map[string]any{"id": "bad", "name": "NoID"},
		map[string]any{"id": json.Number("900003"), "name": "CNY 折扣", "price": map[string]any{
			"currency": "CNY", "final": json.Number("3333"), "initial": json.Number("10000")}},
	}}
	items := steamSearchItems(data, 5)
	if len(items) != 3 {
		t.Fatalf("条数=%v", items)
	}
	a := items[0].(*Obj)
	if a.get("cnyFen") != nil { // 非美元换算不做：US 视图币种不为 CNY → cnyFen 空
		t.Fatalf("USD 币种不应给 cnyFen: %v", a.get("cnyFen"))
	}
	if a.get("discount") != int64(50) {
		t.Fatalf("折扣=%v", a.get("discount"))
	}
	b := items[1].(*Obj)
	if b.get("cnyFen") != int64(4800) || b.get("discount") != int64(50) {
		t.Fatalf("CNY=%v 折扣=%v", b.get("cnyFen"), b.get("discount"))
	}
	c := items[2].(*Obj)
	if c.get("discount") != int64(67) { // round((1-3333/10000)*100)=67
		t.Fatalf("奇数折扣=%v", c.get("discount"))
	}
	// limit 截断 + 无效 id 跳过。
	if items := steamSearchItems(data, 2); len(items) != 2 {
		t.Fatalf("limit 截断=%v", items)
	}
}

func TestSearchSteamShape(t *testing.T) {
	env := newOpsEnv(t)
	// 空态 1：空白词 → bad_term。
	payload := opsExecute(t, env, "search_steam", `{"term": "  "}`)
	if payload["note"] != "bad_term" {
		t.Fatalf("bad_term=%v", payload)
	}
	// 空态 2：离线检索失败（代理指到拒绝端口）→ search_failed。
	env.deps.ResolveProxy = func(ctx context.Context) (string, bool) { return "http://127.0.0.1:1", true }
	payload = opsExecute(t, env, "search_steam", `{"term": "stardew"}`)
	if payload["note"] != "search_failed" {
		t.Fatalf("离线=%v", payload)
	}
}

// ─── 目录导入 / 价格补抓 / 邮件 / 更新 ─────────────────────────────────────

func TestIngestAppidsShape(t *testing.T) {
	env := newOpsEnv(t)
	if _, err := env.db.Exec(`INSERT INTO games (appid, name, updated_at) VALUES (900002, '已在库游戏', '2026-01-01 00:00:00')`); err != nil {
		t.Fatal(err)
	}
	env.runStatus = 200
	payload := opsExecute(t, env, "ingest_appids", `{"appids": [900001, 900002]}`)
	if payload["total"] != float64(2) {
		t.Fatalf("total=%v", payload["total"])
	}
	r0, r1 := rowMap(t, payload, 0), rowMap(t, payload, 1)
	if r0["k"] != "AppID 900001" || r0["vKey"] != "ingestQueued" || r0["v"] != "#900001" || r0["tone"] != "ok" {
		t.Fatalf("行0=%v", r0)
	}
	if r1["k"] != "已在库游戏" || r1["vKey"] != "ingestOwned" {
		t.Fatalf("行1=%v", r1)
	}
	// 空态 1：无有效对象 → no_target。
	payload = opsExecute(t, env, "ingest_appids", `{"appids": [0, -1]}`)
	if payload["note"] != "no_target" {
		t.Fatalf("no_target=%v", payload)
	}
	// 空态 2：超限（21 个不同对象）→ too_many。
	ids := make([]string, 21)
	for i := range ids {
		ids[i] = strconv.Itoa(100000 + i)
	}
	payload = opsExecute(t, env, "ingest_appids", `{"appids": [`+strings.Join(ids, ",")+`]}`)
	if payload["note"] != "too_many" {
		t.Fatalf("too_many=%v", payload)
	}
	// 首爬让位现役任务（409）→ ingestImported。
	env.runStatus = 409
	payload = opsExecute(t, env, "ingest_appids", `{"appids": [900001]}`)
	if rowMap(t, payload, 0)["vKey"] != "ingestImported" {
		t.Fatalf("409=%v", payload)
	}
	// 不可用：依赖未装配。
	env.deps.ImportAppids = nil
	payload = opsExecute(t, env, "ingest_appids", `{"appids": [1]}`)
	if payload["status"] != "error" || payload["note"] != "not_available" {
		t.Fatalf("不可用=%v", payload)
	}
}

func TestRefreshGamePriceShape(t *testing.T) {
	env := newOpsEnv(t)
	if _, err := env.db.Exec(`INSERT INTO games (appid, name) VALUES (900001, '星露谷物语')`); err != nil {
		t.Fatal(err)
	}
	env.runStatus = 200
	payload := opsExecute(t, env, "refresh_game_price", `{"appid": 900001}`)
	r0 := rowMap(t, payload, 0)
	if r0["k"] != "星露谷物语" || r0["vKey"] != "refreshStarted" || r0["tone"] != "ok" {
		t.Fatalf("正例=%v", r0)
	}
	// 空态：占用 409 → refreshBusy；拒绝 400 → refreshRejected。
	env.runStatus = 409
	payload = opsExecute(t, env, "refresh_game_price", `{"appid": 900001}`)
	if rowMap(t, payload, 0)["vKey"] != "refreshBusy" {
		t.Fatalf("busy=%v", payload)
	}
	env.runStatus = 400
	payload = opsExecute(t, env, "refresh_game_price", `{"appid": 900002}`)
	if rowMap(t, payload, 0)["vKey"] != "refreshRejected" || rowMap(t, payload, 0)["k"] != "AppID 900002" {
		t.Fatalf("rejected=%v", payload)
	}
}

func TestNotifyTestShape(t *testing.T) {
	env := newOpsEnv(t)
	env.smtp = map[string]any{"host": "smtp.qq.com", "port": 465, "user": "a@qq.com",
		"password": "a****z", "hasPassword": true, "toAddr": "b@qq.com", "useSsl": true}
	payload := opsExecute(t, env, "notify_test", "")
	if rowMap(t, payload, 0)["vKey"] != "notifySent" || rowMap(t, payload, 0)["tone"] != "ok" {
		t.Fatalf("正例=%v", payload)
	}
	// 空态 1：配置不全 → notifyNoConfig。
	env.smtp = map[string]any{"host": "", "hasPassword": false, "toAddr": ""}
	payload = opsExecute(t, env, "notify_test", "")
	if rowMap(t, payload, 0)["vKey"] != "notifyNoConfig" {
		t.Fatalf("no_config=%v", payload)
	}
	// 空态 2：发送失败 → notifyFailed + 错误文案。
	env.smtp = map[string]any{"host": "smtp.qq.com", "port": 465, "user": "a@qq.com",
		"password": "a****z", "hasPassword": true, "toAddr": "b@qq.com", "useSsl": true}
	env.smtpTestStatus = 400
	payload = opsExecute(t, env, "notify_test", "")
	r0 := rowMap(t, payload, 0)
	if r0["vKey"] != "notifyFailed" || r0["tone"] != "bad" {
		t.Fatalf("failed=%v", r0)
	}
}

func TestUpdateStatusShape(t *testing.T) {
	env := newOpsEnv(t)
	env.version = "2.3.4"
	env.updateProgress = map[string]any{"running": true, "phase": "download", "percent": 55.5,
		"received": 1024, "total": 2048, "error": ""}
	env.updatePending = map[string]any{"pending": true, "tag": "v2.3.5", "swapFailed": false,
		"justUpdated": nil, "installing": false}
	payload := opsExecute(t, env, "update_status", "")
	r0 := rowMap(t, payload, 0)
	if r0["vKey"] != "updateCurrent" || r0["v"] != "2.3.4" {
		t.Fatalf("版本行=%v", r0)
	}
	r1 := rowMap(t, payload, 1)
	if r1["vKey"] != "updateDownloading" {
		t.Fatalf("下载行=%v", r1)
	}
	if payload["currentVersion"] != "2.3.4" || payload["pending"] != true || payload["pendingTag"] != "v2.3.5" {
		t.Fatalf("顶层=%v", payload)
	}
	// 空态：无进度无待装 → 仅当前版本一行。
	env.updateProgress = nil
	env.updatePending = nil
	payload = opsExecute(t, env, "update_status", "")
	rows := payload["rows"].([]any)
	if len(rows) != 1 || payload["pending"] != false {
		t.Fatalf("空态=%v", payload)
	}
}

// ─── 运行时任务（合并读与取消）─────────────────────────────────────────────

func seedOpsTasks(t *testing.T, db *sql.DB) {
	t.Helper()
	ins := func(q string, args ...any) {
		t.Helper()
		if _, err := db.Exec(q, args...); err != nil {
			t.Fatalf("种子写入失败: %v", err)
		}
	}
	// 账本任务（runner=task dev_pytest，live）。
	ins(`INSERT INTO agent_runs (id, session_id, runner, "trigger", meta, status, created_at)
		VALUES ('abc12345', 'runtime', 'task', 'manual', '{"task":"dev_pytest","progress":{"done":1,"total":2}}', 'running', '2026-11-14T07:00:00')`)
	// 价格轮：running（live）+ failed（终态）。
	ins(`INSERT INTO price_cycles (id, kind, status, batches_done, batches_expected, started_at, finished_at) VALUES
		(11, 'scheduled', 'running', 2, 5, '2026-11-14 06:00:00', NULL),
		(10, 'scheduled', 'failed', 1, 5, '2026-11-13 06:00:00', '2026-11-13 06:10:00')`)
	// 抓取任务：repair 独立（live）、missing 独立（终态）、repair 挂轮（排除）。
	ins(`INSERT INTO crawl_jobs (id, kind, status, cycle_id, stats_json, started_at) VALUES
		(21, 'repair', 'running', NULL, '{"processed":3,"total":8,"trigger":"manual"}', '2026-11-14 07:30:00'),
		(20, 'missing', 'done', NULL, '{"processed":5,"total":5}', '2026-11-13 07:30:00'),
		(19, 'repair', 'running', 11, '{"processed":1,"total":2}', '2026-11-14 06:01:00')`)
}

func TestListTasksMergedRead(t *testing.T) {
	env := newOpsEnv(t)
	seedOpsTasks(t, env.db)
	env.clash = map[string]any{"running": false, "activeJobId": 21}
	payload := opsExecute(t, env, "list_tasks", "")
	if payload["total"] != float64(3) {
		t.Fatalf("live total=%v", payload["total"])
	}
	rows := payload["rows"].([]any)
	if len(rows) != 5 { // 3 live + 2 终态（failed cycle + done crawl；挂轮 repair 排除）
		t.Fatalf("行数=%v", payload["rows"])
	}
	// live 在前：dev_pytest（07:00 created）→ crawl:21（07:30 started）→ cycle:11（06:00）按 created_at 降序。
	first := rows[0].(map[string]any)
	if first["run_id"] != "crawl:21" || first["vKey"] != "taskRunning" || first["taskKind"] != "price_repair" {
		t.Fatalf("行0=%v", first)
	}
	if first["v"] != "3/8" {
		t.Fatalf("进度=%v", first["v"])
	}
	if rows[1].(map[string]any)["run_id"] != "abc12345" {
		t.Fatalf("行1=%v", rows[1])
	}
	if rows[2].(map[string]any)["run_id"] != "cycle:11" {
		t.Fatalf("行2=%v", rows[2])
	}
	// 终态行：crawl:20 done → taskDone；cycle:10 failed → taskFailed（created_at 降序）。
	if rows[3].(map[string]any)["vKey"] != "taskDone" || rows[4].(map[string]any)["vKey"] != "taskFailed" {
		t.Fatalf("终态=%v %v", rows[3], rows[4])
	}
	// 挂轮 repair（cycle_id 非空）不得进任务视图。
	for _, raw := range rows {
		if raw.(map[string]any)["run_id"] == "crawl:19" {
			t.Fatalf("挂轮任务混入: %v", rows)
		}
	}
	// 空态：无任务 → taskNoRunning。
	env2 := newOpsEnv(t)
	payload = opsExecute(t, env2, "list_tasks", "")
	if rowMap(t, payload, 0)["vKey"] != "taskNoRunning" || payload["total"] != float64(0) {
		t.Fatalf("空态=%v", payload)
	}
}

func TestCancelTaskShapes(t *testing.T) {
	env := newOpsEnv(t)
	seedOpsTasks(t, env.db)
	env.active = map[string]any{"activeJobId": 21, "busy": true, "maintenance": false, "throttled": 0}
	env.stopStopped = true

	// 按 run_id 停 crawl 任务 → taskStopRequested。
	payload := opsExecute(t, env, "cancel_task", `{"run_id": "crawl:21"}`)
	r0 := rowMap(t, payload, 0)
	if r0["vKey"] != "taskStopRequested" || r0["run_id"] != "crawl:21" {
		t.Fatalf("请求停止=%v", r0)
	}
	// 按 kind 停价格轮（live 里唯一 price_refresh）→ cycle 支路整链停。
	payload = opsExecute(t, env, "cancel_task", `{"kind": "price_refresh"}`)
	if rowMap(t, payload, 0)["vKey"] != "taskStopRequested" {
		t.Fatalf("kind 停轮=%v", payload)
	}
	// 歧义：多个 live 且无参数 → taskAmbiguous。
	payload = opsExecute(t, env, "cancel_task", "")
	if rowMap(t, payload, 0)["vKey"] != "taskAmbiguous" {
		t.Fatalf("歧义=%v", payload)
	}
	// 终态目标：accepted=false → 默认状态行（不请求停止）。
	payload = opsExecute(t, env, "cancel_task", `{"run_id": "crawl:20"}`)
	if rowMap(t, payload, 0)["vKey"] != "taskDone" {
		t.Fatalf("终态=%v", payload)
	}
	// 非活体运行：目标与活体不符 → taskStopUnavailable。
	env.active = map[string]any{"activeJobId": 99, "busy": true, "maintenance": false, "throttled": 0}
	payload = opsExecute(t, env, "cancel_task", `{"run_id": "crawl:21"}`)
	if rowMap(t, payload, 0)["vKey"] != "taskStopUnavailable" || rowMap(t, payload, 0)["tone"] != "warn" {
		t.Fatalf("不可停=%v", payload)
	}
	// 账本任务（runner=task）：Go 无协作取消 → taskStopUnavailable。
	env.active = map[string]any{"activeJobId": 21, "busy": true, "maintenance": false, "throttled": 0}
	payload = opsExecute(t, env, "cancel_task", `{"run_id": "abc12345"}`)
	if rowMap(t, payload, 0)["vKey"] != "taskStopUnavailable" {
		t.Fatalf("账本任务=%v", payload)
	}
	// 空态：无任务 → taskNoRunning。
	env2 := newOpsEnv(t)
	payload = opsExecute(t, env2, "cancel_task", "")
	if rowMap(t, payload, 0)["vKey"] != "taskNoRunning" {
		t.Fatalf("空态=%v", payload)
	}
}
