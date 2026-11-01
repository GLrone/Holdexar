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

// newTestSurface 建真实测试库 + 已迁读路径挂载 + MCP 面（启用态）。
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
	games.Register(mux, db)
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

// executeTool 测试快捷入口：注册表取工具 → 执行 → 返回解码后 payload。
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

// TestRegistryFailClosed fail-closed 投影：只含已实现只读工具，无写组、
// 无会话 / UI 绑定面。
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
		if spec.TimeoutS != budgetDefaultS {
			t.Errorf("%s 预算与现网缺省档不一致: %v", spec.Name, spec.TimeoutS)
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

// TestRegistryDuplicateError 重名即错。
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

// TestGateThreeStates 前缀闸三态：未启用 404；启用后无令牌 / 错令牌 401，
// 对令牌 0 放行；令牌文件持久化（二次装配复用同一令牌）。
func TestGateThreeStates(t *testing.T) {
	_, db, gate, dataDir := newTestSurface(t, true)
	// 未启用面：独立数据目录装配（空设置 + 无旗标 + 无 env）。
	t.Setenv("HOLDEXAR_MCP", "")
	mux2 := http.NewServeMux()
	gate2 := access.New()
	if err := Register(mux2, db, gate2, Options{DataDir: t.TempDir()}); err != nil {
		t.Fatalf("关闭态装配失败: %v", err)
	}

	req := httptest.NewRequest(http.MethodPost, MountPath, nil)
	req.Host = "127.0.0.1:28765"
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

	// 二次装配（模拟重启）：令牌跨进程稳定。
	mux3 := http.NewServeMux()
	gate3 := access.New()
	if err := Register(mux3, db, gate3, Options{DataDir: dataDir, EnabledByFlag: true}); err != nil {
		t.Fatalf("二次装配失败: %v", err)
	}
	req.Header.Set(TokenHeader, token)
	if got := gateCode(gate3, req); got != 0 {
		t.Fatalf("重启后令牌应仍有效: got %d", got)
	}

	// 未启用时令牌文件被清除（同一 mux 重复注册是装配错误，换新 mux）。
	mux4 := http.NewServeMux()
	gate4 := access.New()
	if err := Register(mux4, db, gate4, Options{DataDir: dataDir}); err != nil {
		t.Fatalf("关闭态二次装配失败: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dataDir, TokenFilename)); !os.IsNotExist(err) {
		t.Fatalf("未启用时令牌文件应被清除: %v", err)
	}
}

// gateCode 经完整中间件链取闸判定：放行回 0，拦截回响应状态码。
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

// stubToolSpec 执行器测试用最小工具。
func stubToolSpec(name string, timeout float64, fn HandlerFunc) *ToolSpec {
	return &ToolSpec{Name: name, Group: GroupRead, Risk: "low", Handler: fn, TimeoutS: timeout}
}

// TestExecutorTimeout 超时归一化：budget_s 取整档，绝不外抛。
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

// TestExecutorFailureNormalization 异常 = 结果：error 与 panic 都归一化回灌。
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

// TestExecutorDeny 写工具 + 守卫 → deny（策略面守恒）。
func TestExecutorDeny(t *testing.T) {
	spec := &ToolSpec{Name: "w", Group: GroupWrite, Risk: "low", Handler: func(c *CallContext) (*ToolResult, error) {
		return &ToolResult{Data: newObj()}, nil
	}, TimeoutS: 20}
	res := Execute(&CallContext{Ctx: context.Background()}, spec, true)
	if res.Status != StatusDenied || res.Data.get("note") != "bulk_guard" {
		t.Fatalf("deny 形状不符: %s %s", res.Status, encodeJSON(res.Data))
	}
}

// TestToolHandlersShapes 真实种子库上八工具的形状冒烟（对照验收前的单测防线）。
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

// TestToolHandlersMissing detail / bundles 对不存在游戏回 empty（fail-closed 边界）。
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

// TestReapplyToggle 设置写 mcp.enabled 后的运行时重应用：开→令牌文件与闸放行，
// 关→令牌文件清除、前缀闸回 404；env 强制通道优先于设置关闭。
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

	// env 强制通道：设置关闭不生效（Python enabled = env or settings 同径）
	t.Setenv("HOLDEXAR_MCP", "1")
	Reapply()
	if got := gateCode(gate, req); got != http.StatusUnauthorized {
		t.Fatalf("env 强制开启应 401（无令牌）: got %d", got)
	}
	if _, _, forced = MCPStatus(); !forced {
		t.Fatalf("env 强制应投影 forcedByEnv=true")
	}
}
