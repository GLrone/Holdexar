package agent

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"holdexar/server-go/internal/access"
	"holdexar/server-go/internal/games"
	"holdexar/server-go/internal/notifications"
	"holdexar/server-go/internal/rates"
	"holdexar/server-go/internal/regions"
	"holdexar/server-go/internal/store"
)

const testSchema = `
CREATE TABLE games (
  appid INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  name_en TEXT,
  type TEXT,
  header_image TEXT,
  store_url TEXT,
  chinese_support TEXT,
  family_sharing INTEGER NOT NULL DEFAULT 0,
  trading_cards INTEGER NOT NULL DEFAULT 0,
  xgp_tier TEXT,
  is_epic INTEGER NOT NULL DEFAULT 0,
  epic_date TEXT,
  is_hb INTEGER NOT NULL DEFAULT 0,
  hb_data TEXT,
  bundle_count INTEGER,
  series_id TEXT,
  hl_flag INTEGER NOT NULL DEFAULT 0,
  pp_flag INTEGER NOT NULL DEFAULT 0,
  pp_changed_at TEXT,
  min_cny_fen INTEGER,
  diff_fen INTEGER NOT NULL DEFAULT 0,
  smart_score REAL,
  is_adult INTEGER NOT NULL DEFAULT 0,
  is_visual_novel INTEGER NOT NULL DEFAULT 0,
  release_date TEXT,
  removed_at TEXT,
  free_kind TEXT,
  promo_end_at INTEGER,
  developers TEXT,
  publishers TEXT,
  positive_rate INTEGER,
  positive_reviews INTEGER NOT NULL DEFAULT 0,
  review_count INTEGER NOT NULL DEFAULT 0,
  view_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE game_current_prices (
  appid INTEGER NOT NULL,
  region_code TEXT NOT NULL,
  currency TEXT,
  price INTEGER,
  original_price INTEGER,
  discount_percent INTEGER NOT NULL DEFAULT 0,
  sub_id INTEGER,
  price_status TEXT NOT NULL DEFAULT 'ok',
  fail_count INTEGER NOT NULL DEFAULT 0,
  cny_fen INTEGER,
  discount_end_ts INTEGER,
  attempt_outcome TEXT,
  steam_answer TEXT,
  last_success_at TEXT,
  updated_at TEXT,
  PRIMARY KEY (appid, region_code)
);
CREATE TABLE catalog_removals (appid INTEGER PRIMARY KEY, reason TEXT, removed_at TEXT);
CREATE VIRTUAL TABLE games_name_fts USING fts5(appid UNINDEXED, name_bi);
CREATE TABLE bundles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bundle_id INTEGER NOT NULL UNIQUE,
  name TEXT NOT NULL,
  must_purchase_as_set INTEGER NOT NULL DEFAULT -1,
  item_kind INTEGER NOT NULL DEFAULT -1,
  header_image TEXT,
  min_cny_fen INTEGER,
  diff_fen INTEGER NOT NULL DEFAULT 0,
  url TEXT,
  app_ids TEXT
);
CREATE TABLE bundle_region_prices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bundle_id INTEGER NOT NULL,
  region_code TEXT NOT NULL,
  currency TEXT,
  price INTEGER,
  original_price INTEGER,
  discount_percent INTEGER NOT NULL DEFAULT 0,
  bundle_base_discount INTEGER NOT NULL DEFAULT 0,
  price_status TEXT NOT NULL DEFAULT 'ok',
  cny_fen INTEGER,
  discount_end_ts INTEGER,
  app_ids TEXT,
  crawled_at TEXT
);
CREATE TABLE game_tags (appid INTEGER NOT NULL, tagid INTEGER NOT NULL, weight INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (appid, tagid)) WITHOUT ROWID;
CREATE TABLE tags (tagid INTEGER PRIMARY KEY, name_zh TEXT, name_en TEXT, updated_at TEXT);
CREATE TABLE game_price_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  appid INTEGER NOT NULL,
  region_code TEXT NOT NULL,
  currency TEXT,
  price INTEGER,
  original_price INTEGER,
  discount_percent INTEGER NOT NULL DEFAULT 0,
  sub_id INTEGER,
  is_gold INTEGER NOT NULL DEFAULT 0,
  version_suffix TEXT,
  price_status TEXT NOT NULL DEFAULT 'ok',
  cny_fen INTEGER,
  snapshot_at TEXT
);
CREATE TABLE crawl_regions (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  currency TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  sort INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT
);
CREATE TABLE fx_rates (currency_code TEXT PRIMARY KEY, rate_to_cny REAL NOT NULL DEFAULT 1.0, fetched_at TEXT);
CREATE TABLE fx_rate_history (
	id INTEGER PRIMARY KEY AUTOINCREMENT, currency_code TEXT NOT NULL, rate_date TEXT NOT NULL,
	rate_to_cny REAL NOT NULL, source TEXT, source_kind TEXT, fetched_at TEXT,
	UNIQUE (currency_code, rate_date)
);
CREATE TABLE app_settings (key TEXT PRIMARY KEY, value_json JSON, updated_at DATETIME);
CREATE TABLE crawl_jobs (id INTEGER PRIMARY KEY, status TEXT, cycle_id INTEGER, stats_json TEXT);
CREATE TABLE price_cycles (id INTEGER PRIMARY KEY, status TEXT);
CREATE TABLE notification_candidates (
	id INTEGER PRIMARY KEY AUTOINCREMENT, source_kind VARCHAR(16), event_id INTEGER,
	cycle_id INTEGER, appid INTEGER, region_code VARCHAR(8), event_type VARCHAR(32),
	category VARCHAR(16), recipient VARCHAR(200), status VARCHAR(12), reason TEXT,
	attempts INTEGER, last_error TEXT, created_at DATETIME, updated_at DATETIME,
	delivered_at DATETIME, delivery_history_json JSON, payload_json JSON, due_at DATETIME
);
CREATE TABLE monitor_sources (
	id INTEGER PRIMARY KEY AUTOINCREMENT, target_type TEXT, target_id INTEGER, source TEXT,
	priority INTEGER DEFAULT 0, active INTEGER DEFAULT 1, created_at TEXT, updated_at TEXT
);
CREATE TABLE monitor_targets (
	id INTEGER PRIMARY KEY AUTOINCREMENT, target_type TEXT, target_id INTEGER,
	created_at TEXT, updated_at TEXT
);
CREATE TABLE monitor_exclusions (
	id INTEGER PRIMARY KEY AUTOINCREMENT, target_type TEXT, target_id INTEGER, reason TEXT,
	active INTEGER DEFAULT 1, created_at TEXT, cleared_at TEXT
);
`

func newTestSurface(t *testing.T, flag bool) (*http.ServeMux, *sql.DB, *access.Gate, string) {
	t.Helper()
	dataDir := t.TempDir()
	db, err := store.Open(filepath.Join(dataDir, "holdexar.db"))
	if err != nil {
		t.Fatalf("打开测试库失败: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	for _, stmt := range []string{testSchema} {
		if _, err := db.Exec(stmt); err != nil {
			t.Fatalf("建表失败: %v", err)
		}
	}
	seedTools(t, db)

	mux := http.NewServeMux()
	gate := access.New()
	regions.Register(mux, db)
	rates.Register(mux, db)
	games.Register(mux, db, games.NewHistoryBackfill(context.Background(), db, nil, func(string, ...any) {}), games.Options{})
	notifications.Register(mux, db)
	if err := Register(mux, db, gate, Options{DataDir: dataDir, EnabledByFlag: flag}); err != nil {
		t.Fatalf("MCP 装配失败: %v", err)
	}
	return mux, db, gate, dataDir
}

func seedTools(t *testing.T, db *sql.DB) {
	t.Helper()
	ins := func(q string, args ...any) {
		t.Helper()
		if _, err := db.Exec(q, args...); err != nil {
			t.Fatalf("种子写入失败: %v", err)
		}
	}
	ins(`INSERT INTO games (appid, name, name_en, type, release_date, positive_rate, review_count, hl_flag) VALUES
		(900001, '星露谷物语', 'Stardew Valley', 'GAME', '2016-02-26', 9800, 250000, 0)`)
	ins(`INSERT INTO game_current_prices (appid, region_code, currency, price, discount_percent, price_status, cny_fen, attempt_outcome, updated_at) VALUES
		(900001, 'CN', 'CNY', 4800, 0, 'ok', 4800, 'success', '2026-10-30 09:00:00'),
		(900001, 'JP', 'JPY', 60800, 0, 'ok', NULL, 'success', '2026-10-30 09:00:00')`)
	ins(`INSERT INTO games_name_fts (appid, name_bi) SELECT appid, name_bigram(name, name_en) FROM games`)
	ins(`INSERT INTO crawl_regions (code, name, currency, enabled, sort) VALUES
		('cn', '中国', 'CNY', 1, 0), ('us', '美国', 'USD', 1, 1), ('jp', '日本', 'JPY', 0, 2)`)
	ins(`INSERT INTO fx_rates (currency_code, rate_to_cny) VALUES ('USD', 7.25), ('JPY', 0.048)`)
	ins(`INSERT INTO fx_rate_history (currency_code, rate_date, rate_to_cny, source, source_kind) VALUES
		('USD', '2026-01-01', 7.10, 'test', 'observed'),
		('USD', '2026-01-02', 7.20, 'test', 'observed'),
		('USD', '2026-01-03', 7.30, 'test', 'observed')`)
	ins(`INSERT INTO bundles (bundle_id, name, url, must_purchase_as_set, item_kind, min_cny_fen, diff_fen, app_ids)
		VALUES (501, '星露Bundle', 'https://store.steampowered.com/bundle/501', 1, 0, 3800, 200, '[900001]')`)
	ins(`INSERT INTO bundle_region_prices (bundle_id, region_code, currency, price, cny_fen) VALUES
		(501, 'CN', 'CNY', 4000, 4000), (501, 'US', 'USD', 3800, 3800)`)
	ins(`INSERT INTO app_settings (key, value_json) VALUES
		('notifications.prefs', '{"enabled": true, "quietEnabled": false, "categories": {"price": false}}')`)
	ins(`INSERT INTO notification_candidates (appid, region_code, event_type, category, recipient, status, attempts, delivered_at, updated_at) VALUES
		(900001, 'cn', 'price_drop', 'price', 'user@example.com', 'delivered', 1, '2026-10-30 09:30:00', '2026-10-30 09:30:00')`)
	ins(`INSERT INTO monitor_targets (target_type, target_id) VALUES ('game', 900001)`)
	ins(`INSERT INTO monitor_sources (target_type, target_id, source, active) VALUES ('game', 900001, 'manual', 1)`)
}

func executeTool(t *testing.T, mux *http.ServeMux, db *sql.DB, name string, argsJSON string) map[string]any {
	t.Helper()
	spec := buildRegistryOrDie().get(name)
	if spec == nil {
		t.Fatalf("工具未注册: %s", name)
	}
	var args map[string]any
	if argsJSON != "" {
		if err := json.Unmarshal([]byte(argsJSON), &args); err != nil {
			t.Fatalf("入参解码失败: %v", err)
		}
	}
	fetch := newMuxFetcher(mux)
	res := Execute(&CallContext{Ctx: context.Background(), Args: args, fetch: fetch, db: db}, spec, false)
	var payload map[string]any
	if err := json.Unmarshal([]byte(encodeJSON(payloadWithStatus(res))), &payload); err != nil {
		t.Fatalf("结果解码失败: %v", err)
	}
	return payload
}

func payloadWithStatus(res *ToolResult) *Obj {
	payload := newObj()
	payload.set("status", string(res.Status))
	for i, k := range res.Data.keys {
		payload.set(k, res.Data.vals[i])
	}
	return payload
}

func buildRegistryOrDie() *ToolRegistry {
	r, err := buildRegistry()
	if err != nil {
		panic(err)
	}
	return r
}

func TestRegistryFailClosed(t *testing.T) {
	r := buildRegistryOrDie()
	excluded := map[string]bool{
		"navigate": true, "present_ui": true, "read_attachment": true,
		"find_deletables": true, "propose_delete": true, "propose_bulk": true,
		"start_task": true,
	}
	want := map[string]bool{
		"search_games": true, "recommend_games": true, "game_detail": true,
		"linked_bundles": true, "rates_history": true, "regions_config": true,
		"notifications_status": true, "notification_history": true,
		"list_accounts": true, "list_wishlist": true, "list_owned": true,
		"bills_summary":        true,
		"get_user_preferences": true, "set_user_preference": true,
	}
	for _, name := range []string{
		"top_games", "get_price_briefing", "get_region_prices", "compare_games",
		"diagnose_price", "game_price_history", "game_versions", "bundle_detail",
		"list_bundles", "list_bundle_follows", "rates_overview", "hb_monthly",
		"epic_free", "steam_free", "price_drops", "calendar_events",
		"account_status", "list_follows", "list_alerts", "alert_history",
		"audit_follows_workflow", "achievements_summary", "level_profile",
		"challenges_overview", "achievements_near_completion", "achievements_game_detail",
		"list_family_library", "family_status", "bills_spend_query", "redeem_quota",
		"sync_wallet", "sync_library", "sync_achievements", "sync_bills",
		"sync_family", "refresh_rates", "list_sessions", "read_session",
		"agent_runs_list", "agent_run_events", "list_tools",
		"crawl_active", "price_freshness", "recent_cycles", "price_events",
		"crawl_stop", "recent_job_failures", "write_gate_status", "read_logs",
		"system_diagnostics", "subscriptions_list", "subscription_sync",
		"pool_health_history", "pool_job_runs", "proxy_pool_status",
		"proxy_exit_detail", "proxy_latency_test", "sync_subscriptions",
		"web_search", "refresh_game_price", "notify_test", "update_status",
		"list_tasks", "cancel_task", "search_steam", "ingest_appids",
	} {
		if want[name] {
			t.Errorf("准入清单重名: %s", name)
		}
		want[name] = true
	}
	for _, spec := range r.all() {
		if spec.Group == GroupWrite {
			t.Errorf("写组工具混入注册表: %s", spec.Name)
		}
		if excluded[spec.Name] {
			t.Errorf("会话 / UI 绑定工具混入注册表: %s", spec.Name)
		}
		if !want[spec.Name] {
			t.Errorf("未核对过调用链的工具混入注册表: %s", spec.Name)
		}
		if spec.TimeoutS != budgetFor(spec.Name) {
			t.Errorf("%s 预算与分档表不一致: %v", spec.Name, spec.TimeoutS)
		}
	}
	if len(r.all()) != len(want) {
		t.Errorf("注册数量不符: got %d want %d", len(r.all()), len(want))
	}
	tool := projectTool(r.get("game_detail"))
	if !tool.Annotations.ReadOnlyHint || tool.Annotations.DestructiveHint == nil || *tool.Annotations.DestructiveHint {
		t.Errorf("投影注解不符: %+v", tool.Annotations)
	}
	if tool.Name != "game_detail" || tool.Description == "" || tool.InputSchema == nil {
		t.Errorf("投影字段缺失: %+v", tool)
	}
}

func TestRegistryDuplicateError(t *testing.T) {
	r := newRegistry()
	if err := r.register(&ToolSpec{Name: "x", Group: GroupRead}); err != nil {
		t.Fatalf("首次登记失败: %v", err)
	}
	if err := r.register(&ToolSpec{Name: "x", Group: GroupRead}); err == nil {
		t.Fatal("重名登记未报错")
	} else if !strings.Contains(err.Error(), "重复注册工具: x") {
		t.Fatalf("错误文案不符: %v", err)
	}
}

func TestGateThreeStates(t *testing.T) {
	_, db, gate, dataDir := newTestSurface(t, true)
	t.Setenv("HOLDEXAR_MCP", "")
	mux2 := http.NewServeMux()
	gate2 := access.New()
	if err := Register(mux2, db, gate2, Options{DataDir: t.TempDir()}); err != nil {
		t.Fatalf("关闭态装配失败: %v", err)
	}

	req := httptest.NewRequest(http.MethodPost, MountPath, nil)
	req.Host = "127.0.0.1:28765"
	req.RemoteAddr = "127.0.0.1:40000"
	if got := gateCode(gate2, req); got != http.StatusNotFound {
		t.Fatalf("未启用应 404: got %d", got)
	}
	if got := gateCode(gate, req); got != http.StatusUnauthorized {
		t.Fatalf("无令牌应 401: got %d", got)
	}
	req.Header.Set(TokenHeader, "wrong-token")
	if got := gateCode(gate, req); got != http.StatusUnauthorized {
		t.Fatalf("错令牌应 401: got %d", got)
	}
	tokenRaw, err := os.ReadFile(filepath.Join(dataDir, TokenFilename))
	if err != nil {
		t.Fatalf("令牌文件读取失败: %v", err)
	}
	token := strings.TrimSpace(string(tokenRaw))
	if token == "" {
		t.Fatal("令牌为空")
	}
	req.Header.Set(TokenHeader, token)
	if got := gateCode(gate, req); got != 0 {
		t.Fatalf("对令牌应放行: got %d", got)
	}

	mux3 := http.NewServeMux()
	gate3 := access.New()
	if err := Register(mux3, db, gate3, Options{DataDir: dataDir, EnabledByFlag: true}); err != nil {
		t.Fatalf("二次装配失败: %v", err)
	}
	req.Header.Set(TokenHeader, token)
	if got := gateCode(gate3, req); got != 0 {
		t.Fatalf("重启后令牌应仍有效: got %d", got)
	}

	mux4 := http.NewServeMux()
	gate4 := access.New()
	if err := Register(mux4, db, gate4, Options{DataDir: dataDir}); err != nil {
		t.Fatalf("关闭态二次装配失败: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dataDir, TokenFilename)); !os.IsNotExist(err) {
		t.Fatalf("未启用时令牌文件应被清除: %v", err)
	}
}

func gateCode(g *access.Gate, r *http.Request) int {
	reached := false
	handler := http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) { reached = true })
	rec := httptest.NewRecorder()
	g.Middleware(handler).ServeHTTP(rec, r)
	if reached {
		return 0
	}
	return rec.Code
}

func stubToolSpec(name string, timeout float64, fn HandlerFunc) *ToolSpec {
	return &ToolSpec{Name: name, Group: GroupRead, Risk: "low", Handler: fn, TimeoutS: timeout}
}

func TestExecutorTimeout(t *testing.T) {
	spec := stubToolSpec("slow", 0.05, func(c *CallContext) (*ToolResult, error) {
		time.Sleep(200 * time.Millisecond)
		return &ToolResult{Data: newObj()}, nil
	})
	res := Execute(&CallContext{Ctx: context.Background()}, spec, false)
	if res.Status != StatusTimeout || res.ErrCode != "timeout" {
		t.Fatalf("超时态不符: %s %s", res.Status, res.ErrCode)
	}
	if res.Data.get("kind") != "timeout" || res.Data.get("tool") != "slow" || res.Data.get("budget_s") != 0 {
		t.Fatalf("超时载荷不符: %s", encodeJSON(res.Data))
	}
}

func TestExecutorFailureNormalization(t *testing.T) {
	spec := stubToolSpec("boom", 20, func(c *CallContext) (*ToolResult, error) {
		return nil, errors.New("数据库不可用")
	})
	res := Execute(&CallContext{Ctx: context.Background()}, spec, false)
	if res.Status != StatusError || res.ErrCode != "tool_error" {
		t.Fatalf("失败态不符: %s %s", res.Status, res.ErrCode)
	}
	if res.Data.get("kind") != "failed" || res.Data.get("tool") != "boom" {
		t.Fatalf("失败载荷不符: %s", encodeJSON(res.Data))
	}
	if note, _ := res.Data.get("note").(string); !strings.Contains(note, "error") {
		t.Fatalf("注记应含错误类型名: %v", res.Data.get("note"))
	}

	specPanic := stubToolSpec("panic", 20, func(c *CallContext) (*ToolResult, error) {
		panic("炸了")
	})
	res = Execute(&CallContext{Ctx: context.Background()}, specPanic, false)
	if res.Status != StatusError || res.Data.get("kind") != "failed" {
		t.Fatalf("panic 未归一化: %s %s", res.Status, encodeJSON(res.Data))
	}
}

func TestExecutorDeny(t *testing.T) {
	spec := &ToolSpec{Name: "w", Group: GroupWrite, Risk: "low", Handler: func(c *CallContext) (*ToolResult, error) {
		return &ToolResult{Data: newObj()}, nil
	}, TimeoutS: 20}
	res := Execute(&CallContext{Ctx: context.Background()}, spec, true)
	if res.Status != StatusDenied || res.Data.get("note") != "bulk_guard" {
		t.Fatalf("deny 形状不符: %s %s", res.Status, encodeJSON(res.Data))
	}
}

func TestToolHandlersShapes(t *testing.T) {
	mux, db, _, _ := newTestSurface(t, true)

	payload := executeTool(t, mux, db, "search_games", `{"q": "星露"}`)
	items := payload["items"].([]any)
	if len(items) != 1 {
		t.Fatalf("检索命中数不符: %v", payload["items"])
	}
	first := items[0].(map[string]any)
	if v, ok := first["appid"].(float64); !ok || v != 900001 {
		t.Fatalf("检索行 appid 不符: %v", first)
	}
	if v, ok := first["basePriceFen"].(float64); !ok || v != 4800 {
		t.Fatalf("检索行 basePriceFen 不符: %v", first)
	}
	if v, ok := first["positiveRate"].(float64); !ok || v != 98 {
		t.Fatalf("检索行 positiveRate 不符: %v", first)
	}
	if _, ok := first["cnyFen"]; ok {
		t.Fatal("检索原始行不应带 cnyFen 键")
	}

	payload = executeTool(t, mux, db, "recommend_games", `{"only_discounted": false, "min_rating": 90}`)
	if _, ok := payload["items"].([]any); !ok {
		t.Fatalf("推荐应回 items: %v", payload)
	}

	payload = executeTool(t, mux, db, "game_detail", `{"appid": 900001}`)
	if payload["kind"] != "game_detail" || payload["tracked"] != true {
		t.Fatalf("详情形状不符: %v", payload)
	}
	cn := payload["cn"].(map[string]any)
	if cn["cnyFen"].(float64) != 4800 || cn["discount"].(float64) != 0 {
		t.Fatalf("国区价不符: %v", cn)
	}
	if payload["priceRegionCount"].(float64) != 2 {
		t.Fatalf("价格区计数不符: %v", payload["priceRegionCount"])
	}

	payload = executeTool(t, mux, db, "linked_bundles", `{"appid": 900001}`)
	if payload["kind"] != "bundles" || payload["total"].(float64) != 1 {
		t.Fatalf("捆绑包形状不符: %v", payload)
	}
	bundle := payload["items"].([]any)[0].(map[string]any)
	if bundle["bundleId"].(float64) != 501 || bundle["priceCny"].(float64) != 4000 {
		t.Fatalf("捆绑包条目不符: %v", bundle)
	}

	payload = executeTool(t, mux, db, "rates_history", `{"currency": "usd", "days": 90}`)
	if payload["kind"] != "rates_history" || payload["count"].(float64) != 3 {
		t.Fatalf("汇率形状不符: %v", payload)
	}
	if payload["min"].(float64) != 7.10 || payload["last"].(float64) != 7.30 {
		t.Fatalf("汇率极值不符: %v", payload)
	}

	payload = executeTool(t, mux, db, "regions_config", ``)
	if payload["enabledCount"].(float64) != 2 {
		t.Fatalf("启用区计数不符: %v", payload)
	}
	if payload["ownedRegions"] != nil {
		t.Fatalf("已购抓取区缺省应 null: %v", payload["ownedRegions"])
	}

	payload = executeTool(t, mux, db, "notifications_status", ``)
	if payload["enabled"] != true || payload["delivered"].(float64) != 1 {
		t.Fatalf("通知状态形状不符: %v", payload)
	}
	disabled := payload["disabledCategories"].([]any)
	if len(disabled) != 1 || disabled[0] != "price" {
		t.Fatalf("停用类别不符: %v", disabled)
	}

	payload = executeTool(t, mux, db, "notification_history", `{"limit": 5}`)
	rows := payload["rows"].([]any)
	if len(rows) != 1 || payload["total"].(float64) != 1 {
		t.Fatalf("通知历史形状不符: %v", payload)
	}
	row := rows[0].(map[string]any)
	if row["k"] != "price_drop·cn" || row["v"] != "u***@example.com" {
		t.Fatalf("通知历史行不符: %v", row)
	}
}

func TestToolHandlersMissing(t *testing.T) {
	mux, db, _, _ := newTestSurface(t, true)
	for _, tc := range []struct {
		tool, args, note string
	}{
		{"game_detail", `{"appid": 42}`, "no_game"},
		{"linked_bundles", `{"appid": 42}`, "no_data"},
		{"linked_bundles", `{"appid": 0}`, "no_data"},
		{"rates_history", `{"currency": "GBP", "days": 90}`, "no_data"},
	} {
		payload := executeTool(t, mux, db, tc.tool, tc.args)
		if payload["kind"] != "empty" || payload["note"] != tc.note {
			t.Fatalf("%s 缺席边界不符: %v", tc.tool, payload)
		}
	}
}

func TestReapplyToggle(t *testing.T) {
	t.Setenv("HOLDEXAR_MCP", "")
	_, db, gate, dataDir := newTestSurface(t, false)
	setEnabled := func(on bool) {
		v := "false"
		if on {
			v = "true"
		}
		if _, err := db.Exec(`INSERT INTO app_settings (key, value_json, updated_at)
			VALUES ('mcp.enabled', ?, '2026-01-01 00:00:00.000000')
			ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json`, v); err != nil {
			t.Fatalf("设置写入失败：%v", err)
		}
	}
	req := httptest.NewRequest(http.MethodPost, MountPath, nil)
	req.Host = "127.0.0.1:28765"
	req.RemoteAddr = "127.0.0.1:40000"

	if got := gateCode(gate, req); got != http.StatusNotFound {
		t.Fatalf("初始关闭应 404: got %d", got)
	}
	setEnabled(true)
	Reapply()
	if got := gateCode(gate, req); got != http.StatusUnauthorized {
		t.Fatalf("开启后无令牌应 401: got %d", got)
	}
	tokenRaw, err := os.ReadFile(filepath.Join(dataDir, TokenFilename))
	if err != nil {
		t.Fatalf("令牌文件应落盘: %v", err)
	}
	req.Header.Set(TokenHeader, strings.TrimSpace(string(tokenRaw)))
	if got := gateCode(gate, req); got != 0 {
		t.Fatalf("开启后对令牌应放行: got %d", got)
	}
	running, errText, forced := MCPStatus()
	if !running || errText != "" || forced {
		t.Fatalf("运行态投影不符: running=%v err=%q forced=%v", running, errText, forced)
	}

	setEnabled(false)
	Reapply()
	if got := gateCode(gate, req); got != http.StatusNotFound {
		t.Fatalf("关闭后应回 404: got %d", got)
	}
	if _, err := os.Stat(filepath.Join(dataDir, TokenFilename)); !os.IsNotExist(err) {
		t.Fatalf("关闭后令牌文件应被清除: %v", err)
	}
	if running, _, _ = MCPStatus(); running {
		t.Fatalf("关闭后运行态应为未在役")
	}

	t.Setenv("HOLDEXAR_MCP", "1")
	Reapply()
	if got := gateCode(gate, req); got != http.StatusUnauthorized {
		t.Fatalf("env 强制开启应 401（无令牌）: got %d", got)
	}
	if _, _, forced = MCPStatus(); !forced {
		t.Fatalf("env 强制应投影 forcedByEnv=true")
	}
}

func registerOne(t *testing.T, spec *ToolSpec) *ToolRegistry {
	t.Helper()
	r := newRegistry()
	if err := r.register(spec); err != nil {
		t.Fatalf("登记失败: %v", err)
	}
	return r
}

func TestValidateArgsFullSchema(t *testing.T) {
	cases := []struct {
		name      string
		spec      *ToolSpec
		args      map[string]any
		wantPass  bool
		wantParts []string
	}{
		{
			name:      "缺必填",
			spec:      &ToolSpec{Name: "t", Parameters: `{"type":"object","properties":{"q":{"type":"string"}},"required":["q"]}`},
			args:      map[string]any{},
			wantParts: []string{"t 参数校验失败", `missing properties: ["q"]`},
		},
		{
			name:      "类型错误",
			spec:      &ToolSpec{Name: "t", Parameters: `{"type":"object","properties":{"appid":{"type":"integer"}},"required":["appid"]}`},
			args:      map[string]any{"appid": "abc"},
			wantParts: []string{"/properties/appid", `want "integer"`},
		},
		{
			name:     "json.Number 归一后过 integer 校验",
			spec:     &ToolSpec{Name: "t", Parameters: `{"type":"object","properties":{"appid":{"type":"integer"}},"required":["appid"]}`},
			args:     map[string]any{"appid": json.Number("730")},
			wantPass: true,
		},
		{
			name:      "enum 违例",
			spec:      &ToolSpec{Name: "t", Parameters: `{"type":"object","properties":{"level":{"type":"string","enum":["info","warning","error"]}}}`},
			args:      map[string]any{"level": "debug"},
			wantParts: []string{"/properties/level", "enum:"},
		},
		{
			name:      "minimum 违例",
			spec:      &ToolSpec{Name: "t", Parameters: `{"type":"object","properties":{"days":{"type":"integer","minimum":1}}}`},
			args:      map[string]any{"days": 0},
			wantParts: []string{"/properties/days", "minimum:"},
		},
		{
			name:     "未声明额外键放行",
			spec:     &ToolSpec{Name: "t", Parameters: `{"type":"object","properties":{"q":{"type":"string"}},"required":["q"]}`},
			args:     map[string]any{"q": "portal", "extra": 1},
			wantPass: true,
		},
		{
			name:     "合法参数通过",
			spec:     &ToolSpec{Name: "t", Parameters: `{"type":"object","properties":{"q":{"type":"string"},"days":{"type":"integer"}},"required":["q"]}`},
			args:     map[string]any{"q": "portal", "days": 90},
			wantPass: true,
		},
		{
			name:      "数组元素类型错误",
			spec:      &ToolSpec{Name: "t", Parameters: `{"type":"object","properties":{"appids":{"type":"array","items":{"type":"integer"}}}}`},
			args:      map[string]any{"appids": []any{730, "x"}},
			wantParts: []string{"/properties/appids/items", `want "integer"`},
		},
	}
	for _, tc := range cases {
		reg := registerOne(t, tc.spec)
		err := validateArgs(reg, tc.spec, tc.args)
		if tc.wantPass {
			if err != nil {
				t.Errorf("%s: 应放行: %v", tc.name, err)
			}
			continue
		}
		if err == nil {
			t.Errorf("%s: 应报错", tc.name)
			continue
		}
		for _, part := range tc.wantParts {
			if !strings.Contains(err.Error(), part) {
				t.Errorf("%s: 报错缺 %q: %v", tc.name, part, err)
			}
		}
	}
}

func TestValidateArgsUnresolvableFallback(t *testing.T) {
	// schema 可解析但引用悬空：回退必需键校验，且失败原因留在 registry 供上报。
	spec := &ToolSpec{Name: "t", Parameters: `{"type":"object","$ref":"#/missing","required":["q"]}`}
	reg := registerOne(t, spec)
	if len(reg.schemaFailures()) == 0 {
		t.Fatal("Resolve 失败应登记")
	}
	if err := validateArgs(reg, spec, map[string]any{}); err == nil {
		t.Fatal("兜底仍应校验 required")
	} else if !strings.Contains(err.Error(), `缺少必需参数 "q"`) {
		t.Fatalf("兜底文案不符: %v", err)
	}
	if err := validateArgs(reg, spec, map[string]any{"q": "portal"}); err != nil {
		t.Fatalf("兜底全参应放行: %v", err)
	}
	broken := &ToolSpec{Name: "x", Parameters: `{not-json`}
	breg := registerOne(t, broken)
	if len(breg.schemaFailures()) == 0 {
		t.Fatal("不可解析 schema 应登记")
	}
	if err := validateArgs(breg, broken, map[string]any{}); err != nil {
		t.Fatalf("required 不可解析应放行: %v", err)
	}
}

func TestCallHandlerInvalidArgsIsError(t *testing.T) {
	reg := registerOne(t, &ToolSpec{
		Name: "echo", Group: GroupRead, TimeoutS: 5,
		Parameters: `{"type":"object","properties":{"q":{"type":"string"}},"required":["q"]}`,
		Handler: func(c *CallContext) (*ToolResult, error) {
			out := newObj()
			out.set("kind", "echo")
			return &ToolResult{Status: StatusOK, Data: out}, nil
		},
	})
	s := &Surface{registry: reg}
	spec := reg.get("echo")

	bad := &mcp.CallToolRequest{Params: &mcp.CallToolParamsRaw{
		Name: "echo", Arguments: json.RawMessage(`{"q":42}`),
	}}
	res, err := s.callHandler(spec)(context.Background(), bad)
	if err != nil {
		t.Fatalf("校验失败应回 isError 而非协议错误: %v", err)
	}
	if !res.IsError {
		t.Fatalf("类型错误应回 isError: %+v", res)
	}
	if len(res.Content) == 0 {
		t.Fatal("isError 应带文案")
	}
	if text, ok := res.Content[0].(*mcp.TextContent); !ok ||
		!strings.Contains(text.Text, `参数校验失败`) || !strings.Contains(text.Text, `want "string"`) {
		t.Fatalf("文案不符: %+v", res.Content[0])
	}

	good := &mcp.CallToolRequest{Params: &mcp.CallToolParamsRaw{
		Name: "echo", Arguments: json.RawMessage(`{"q":"portal","spare":true}`),
	}}
	res, err = s.callHandler(spec)(context.Background(), good)
	if err != nil {
		t.Fatalf("合法调用不应出错: %v", err)
	}
	if res.IsError {
		t.Fatalf("合法参数不应 isError: %+v", res)
	}
}
