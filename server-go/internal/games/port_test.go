package games

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"holdexar/server-go/internal/monitoring"
	"holdexar/server-go/internal/store"
)

// games 域移植端点契约测试（对齐 GAMES_PORT_SPEC §3 断言面）：
// 榜单读面、价格上下文、版本聚合、系列成员、CDK 查价缓存、目录写三件。

const portSchema = `
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
  removed_strikes INTEGER NOT NULL DEFAULT 0,
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
  is_bundle INTEGER NOT NULL DEFAULT 0,
  product_unknown INTEGER NOT NULL DEFAULT 0,
  price_status TEXT NOT NULL DEFAULT 'ok',
  cny_fen INTEGER,
  snapshot_at TEXT
);
CREATE TABLE monitor_targets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_type TEXT NOT NULL,
  target_id INTEGER NOT NULL,
  created_at TEXT, updated_at TEXT, activated_at TEXT, released_at TEXT, excluded_at TEXT,
  UNIQUE (target_type, target_id)
);
CREATE TABLE monitor_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_type TEXT NOT NULL,
  target_id INTEGER NOT NULL,
  source TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 10,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE monitor_exclusions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_type TEXT NOT NULL,
  target_id INTEGER NOT NULL,
  reason TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT, cleared_at TEXT
);
CREATE UNIQUE INDEX ux_monitor_exclusion_active ON monitor_exclusions (target_type, target_id) WHERE active = 1;
CREATE UNIQUE INDEX ux_monitor_source ON monitor_sources (target_type, target_id, source);
CREATE TABLE wishlist_items (
  steamid TEXT NOT NULL,
  appid INTEGER NOT NULL,
  added_at TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  owned INTEGER NOT NULL DEFAULT 0,
  manual INTEGER NOT NULL DEFAULT 0,
  wishlisted INTEGER NOT NULL DEFAULT 0,
  manual_pool INTEGER NOT NULL DEFAULT 0,
  excluded INTEGER NOT NULL DEFAULT 0,
  board_pool INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (steamid, appid)
);
CREATE TABLE fx_rates (currency_code TEXT PRIMARY KEY, rate_to_cny REAL NOT NULL DEFAULT 1.0, fetched_at TEXT);
CREATE TABLE crawl_jobs (id INTEGER PRIMARY KEY, status TEXT, cycle_id INTEGER, stats_json TEXT);
CREATE TABLE price_cycles (id INTEGER PRIMARY KEY, status TEXT);
CREATE TABLE crawl_regions (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  currency TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  sort INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT
);
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
`

type jobStub struct {
	mu    sync.Mutex
	calls []string // "kind|scope|ids"
	next  int64
	err   error
}

func (s *jobStub) start(_ context.Context, scope string, ids []int64, kind string) (map[string]any, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	idStrings := make([]string, len(ids))
	for i, id := range ids {
		idStrings[i] = fmt.Sprint(id)
	}
	s.calls = append(s.calls, kind+"|"+scope+"|"+strings.Join(idStrings, ","))
	if s.err != nil {
		return nil, s.err
	}
	s.next++
	return map[string]any{"id": s.next, "scope": scope, "count": len(ids)}, nil
}

func (s *jobStub) hasCall(kind string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, c := range s.calls {
		if strings.HasPrefix(c, kind+"|") {
			return true
		}
	}
	return false
}

func (s *jobStub) callsOf(kind string) []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := []string{}
	for _, c := range s.calls {
		if strings.HasPrefix(c, kind+"|") {
			out = append(out, c)
		}
	}
	return out
}

type boardStub struct {
	boards map[string][]int64
}

func (b *boardStub) get(_ context.Context, key string) ([]int64, error) {
	ids, ok := b.boards[key]
	if !ok {
		return nil, fmt.Errorf("未知榜单源: %s", key)
	}
	return ids, nil
}

// newPortHandler 建独立临时库 + 完整装配（榜单桩 / 写链桩 / StartJob 桩）。
func newPortHandler(t *testing.T) (*httptest.Server, *sql.DB, *jobStub, *boardStub) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "test.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatalf("打开测试库失败: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	if _, err = db.Exec(portSchema); err != nil {
		t.Fatalf("建表失败: %v", err)
	}
	seedPortData(t, db)

	gate := store.NewWriteGate(db, path)
	monitor := monitoring.New(db, gate)
	jobs := &jobStub{}
	boards := &boardStub{boards: map[string][]int64{
		"topsellers": {101, 202},
		"popularnew": {303},
		"specials":   {},
	}}
	mux := http.NewServeMux()
	Register(mux, db, nil, Options{
		BoardGet: boards.get,
		Writes: &WriteDeps{
			Gate: gate, Monitor: monitor,
			StartJob: jobs.start,
			Log:      func(string, ...any) {},
		},
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv, db, jobs, boards
}

func seedPortData(t *testing.T, db *sql.DB) {
	t.Helper()
	exec := func(q string, args ...any) {
		t.Helper()
		if _, err := db.Exec(q, args...); err != nil {
			t.Fatalf("种子写入失败: %v", err)
		}
	}
	exec(`INSERT INTO games (appid, name, type, series_id, min_cny_fen, diff_fen) VALUES
		(900001, '只狼：影逝二度', 'GAME', 'onlywolf', 43000, 16700),
		(900002, 'Hollow Knight', 'GAME', '', NULL, 0),
		(900003, '星露谷物语', 'GAME', '', 2918, 1882),
		(900004, '只狼外传', 'GAME', 'onlywolf', NULL, 0)`)

	exec(`INSERT INTO game_current_prices (appid, region_code, currency, price, original_price,
		discount_percent, sub_id, price_status, cny_fen, updated_at) VALUES
		(900001, 'CN', 'CNY', 26800, 29800, 1000, 9, 'ok', 26800, '2026-10-30 09:00:00'),
		(900001, 'JP', 'JPY', 60800, NULL, 0, 3, 'ok', NULL, '2026-10-30 09:00:00'),
		(900003, 'CN', 'CNY', 4900, 4800, 200, 5, 'ok', 4900, '2026-10-30 09:00:00'),
		(900002, 'CN', 'CNY', 4800, NULL, 0, 8, 'ok', 4800, '2026-10-30 09:00:00')`)

	exec(`INSERT INTO fx_rates (currency_code, rate_to_cny) VALUES ('USD', 7.25), ('JPY', 0.048)`)

	exec(`INSERT INTO game_price_history (appid, region_code, currency, price, discount_percent,
		sub_id, is_gold, version_suffix, is_bundle, price_status, cny_fen, snapshot_at) VALUES
		(900003, 'CN', 'CNY', 4800, 0, 1, 0, NULL, 0, 'ok', 4800, '2024-01-01 10:00:00'),
		(900003, 'CN', 'CNY', 4000, 2000, 1, 0, NULL, 0, 'ok', 4000, '2024-01-05 10:00:00'),
		(900003, 'CN', 'CNY', 6800, 0, 2, 1, '黄金版', 0, 'ok', 6800, '2024-01-08 10:00:00'),
		(900003, 'CN', 'CNY', 3900, 1900, 5, 0, NULL, 0, 'ok', 3900, '2024-01-09 10:00:00'),
		(900003, 'CN', 'CNY', 3600, 2500, 1, 0, NULL, 0, 'ok', 3600, '2024-01-10 10:00:00'),
		(900003, 'CN', 'CNY', 5200, 0, 1, 0, NULL, 0, 'ok', 5200, '2024-01-20 10:00:00'),
		(900003, 'CN', 'CNY', 100, 0, 3, 0, NULL, 1, 'ok', 100, '2024-01-02 10:00:00'),
		(900003, 'US', 'USD', 1499, 0, 1, 0, NULL, 0, 'ok', NULL, '2024-01-05 10:00:00')`)

	exec(`INSERT INTO wishlist_items (steamid, appid, added_at, active, manual, wishlisted)
		VALUES ('a', 900001, '2026-10-01', 1, 1, 1)`)
}

func postJSON(t *testing.T, srv *httptest.Server, path, body string) (int, map[string]any) {
	t.Helper()
	resp, err := http.Post(srv.URL+path, "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatalf("请求失败 %s: %v", path, err)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("响应读取失败 %s: %v", path, err)
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatalf("响应解析失败 %s: %s", path, raw)
	}
	return resp.StatusCode, m
}

// ── 榜单读面 ─────────────────────────────────────────────────────────────

func TestTop100Shape(t *testing.T) {
	srv, _, _, _ := newPortHandler(t)
	status, body := getJSON(t, srv, "/api/v1/games/top100")
	if status != 200 {
		t.Fatalf("状态码 %d: %v", status, body)
	}
	ids, ok := body["appIds"].([]any)
	if !ok || len(ids) != 2 || ids[0] != "101" || ids[1] != "202" {
		t.Fatalf("appIds 应为字符串数组: %v", body["appIds"])
	}
	if body["cached"] != true {
		t.Fatalf("cached 恒 true: %v", body)
	}
}

func TestBoardKeyRoutes(t *testing.T) {
	srv, _, _, _ := newPortHandler(t)
	status, body := getJSON(t, srv, "/api/v1/games/boards/topsellers")
	if status != 200 || body["board"] != "topsellers" || body["count"].(float64) != 2 {
		t.Fatalf("榜单形状异常: %d %v", status, body)
	}
	if ids := body["appIds"].([]any); len(ids) != 2 || ids[0] != "101" {
		t.Fatalf("榜单 appIds 异常: %v", body["appIds"])
	}

	status, body = getJSON(t, srv, "/api/v1/games/boards/bogus")
	if status != 404 || body["detail"] != "未知榜单源: bogus" {
		t.Fatalf("未知榜单源应 404 同文案: %d %v", status, body)
	}

	status, body = getJSON(t, srv, "/api/v1/games/boards/specials")
	if status != 502 || body["detail"] != "FETCH_FAILED" {
		t.Fatalf("空榜应 502 FETCH_FAILED: %d %v", status, body)
	}
}

func TestGamesRoutesMethodAndShadowing(t *testing.T) {
	srv, _, _, _ := newPortHandler(t)
	resp, err := http.Post(srv.URL+"/api/v1/games/top100", "application/json", strings.NewReader("{}"))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("错误方法应 405: %d", resp.StatusCode)
	}

	// 字面路由优先于 {appid}；其余段仍走详情 404。
	status, body := getJSON(t, srv, "/api/v1/games/top100")
	if status != 200 {
		t.Fatalf("top100 被详情路由遮蔽: %d %v", status, body)
	}
	// GET /games/remove 落回 GET /{appid} → appid 非整数 422。
	status, body = getJSON(t, srv, "/api/v1/games/remove")
	if status != 422 {
		t.Fatalf("GET remove 应 422 int_parsing: %d %v", status, body)
	}
}

// ── 价格上下文 ───────────────────────────────────────────────────────────

func TestPriceContextWindow(t *testing.T) {
	srv, _, _, _ := newPortHandler(t)

	status, body := getJSON(t, srv, "/api/v1/games/900003/price-context?date=2024-01-15")
	if status != 200 {
		t.Fatalf("状态码 %d: %v", status, body)
	}
	if body["region"] != "cn" || body["appid"].(float64) != 900003 {
		t.Fatalf("region/appid 异常: %v", body)
	}
	at := body["at"].(map[string]any)
	if at["cnyFen"].(float64) != 3600 || at["discount"].(float64) != 2500 ||
		at["snapshotAt"] != "2024-01-10T10:00:00" {
		t.Fatalf("当时价应为窗口末点(2024-01-10 3600): %v", at)
	}
	lowest := body["lowest"].(map[string]any)
	if lowest["cnyFen"].(float64) != 3600 {
		t.Fatalf("史低应为窗口最小 3600: %v", lowest)
	}

	//日后切片不漏入
	_, body = getJSON(t, srv, "/api/v1/games/900003/price-context?date=2024-01-10")
	if body["at"].(map[string]any)["cnyFen"].(float64) != 3600 {
		t.Fatalf("窗口末点应为 3600: %v", body["at"])
	}

	// 窗口早期：只有首点
	_, body = getJSON(t, srv, "/api/v1/games/900003/price-context?date=2024-01-04")
	at = body["at"].(map[string]any)
	if at["cnyFen"].(float64) != 4800 || at["snapshotAt"] != "2024-01-01T10:00:00" {
		t.Fatalf("早期窗口应为 2024-01-01 4800: %v", at)
	}

	// 非标准版不混入（gold 行 01-08 不进窗口）；在售 sub 联合（01-09 3900 进窗口）
	_, body = getJSON(t, srv, "/api/v1/games/900003/price-context?date=2024-01-09")
	at = body["at"].(map[string]any)
	if at["cnyFen"].(float64) != 3900 {
		t.Fatalf("在售 sub 应经并集进入窗口: %v", at)
	}

	// 空窗与非法日期均回 null 对（不报错）
	for _, date := range []string{"2023-06-01", "2035-13-45"} {
		_, body = getJSON(t, srv, "/api/v1/games/900003/price-context?date="+date)
		if body["at"] != nil || body["lowest"] != nil {
			t.Fatalf("date=%s 应回 null 对: %v", date, body)
		}
	}
}

func TestPriceContextValidation(t *testing.T) {
	srv, _, _, _ := newPortHandler(t)
	status, body := getJSON(t, srv, "/api/v1/games/900003/price-context")
	if status != 422 {
		t.Fatalf("缺 date 应 422: %d %v", status, body)
	}
	detail := body["detail"].([]any)
	first := detail[0].(map[string]any)
	if first["type"] != "missing" || fmt.Sprint(first["loc"]) != "[query date]" || first["msg"] != "Field required" {
		t.Fatalf("缺 date 形状异常: %v", first)
	}

	status, body = getJSON(t, srv, "/api/v1/games/900003/price-context?date=2024-01")
	detail = body["detail"].([]any)
	first = detail[0].(map[string]any)
	if first["type"] != "string_too_short" || first["ctx"].(map[string]any)["min_length"].(float64) != 10 {
		t.Fatalf("短 date 形状异常: %v", first)
	}

	status, body = getJSON(t, srv, "/api/v1/games/900003/price-context?date=2024-01-011&region=abcdef")
	detail = body["detail"].([]any)
	if len(detail) != 2 {
		t.Fatalf("date+region 错误应累积: %v", detail)
	}
	if detail[0].(map[string]any)["type"] != "string_too_long" {
		t.Fatalf("首错应为 date too_long: %v", detail[0])
	}
	if detail[1].(map[string]any)["ctx"].(map[string]any)["max_length"].(float64) != 5 {
		t.Fatalf("region 上限应 5: %v", detail[1])
	}
}

func TestPriceContextBatch(t *testing.T) {
	srv, _, _, _ := newPortHandler(t)
	status, body := postJSON(t, srv, "/api/v1/games/price-context-batch",
		`{"items":[{"appid":900003,"date":"2024-01-15"},{"appid":0,"date":""},{"appid":900003,"date":""},{"appid":900003,"date":"2024-01-04"}]}`)
	if status != 200 {
		t.Fatalf("状态码 %d: %v", status, body)
	}
	results := body["results"].([]any)
	if len(results) != 4 {
		t.Fatalf("顺序应保持 4 项: %v", results)
	}
	if results[0].(map[string]any)["at"] == nil {
		t.Fatalf("首项应命中价格段: %v", results[0])
	}
	for _, idx := range []int{1, 2} {
		pair := results[idx].(map[string]any)
		if pair["at"] != nil || pair["lowest"] != nil {
			t.Fatalf("坏对应回 null 对: %v", pair)
		}
		if _, has := pair["region"]; has {
			t.Fatalf("null 对不应带 region 键: %v", pair)
		}
	}
	if results[3].(map[string]any)["at"].(map[string]any)["cnyFen"].(float64) != 4800 {
		t.Fatalf("末项应为 2024-01-04 窗口: %v", results[3])
	}

	// 缺 items → 422 loc ["body","items"]
	status, body = postJSON(t, srv, "/api/v1/games/price-context-batch", `{}`)
	detail := body["detail"].([]any)
	if status != 422 || detail[0].(map[string]any)["type"] != "missing" {
		t.Fatalf("缺 items 形状异常: %d %v", status, detail)
	}
	// 元素类型错累积
	status, body = postJSON(t, srv, "/api/v1/games/price-context-batch", `{"items":[1,{"appid":"x","date":5}]}`)
	detail = body["detail"].([]any)
	if status != 422 || len(detail) != 3 {
		t.Fatalf("元素错误应累积 3 条: %d %v", status, detail)
	}
	if detail[0].(map[string]any)["type"] != "model_attributes_type" ||
		detail[1].(map[string]any)["type"] != "int_parsing" ||
		detail[2].(map[string]any)["type"] != "string_type" {
		t.Fatalf("元素错误形状异常: %v", detail)
	}
	// ≤200 对截断
	items := make([]string, 201)
	for i := range items {
		items[i] = fmt.Sprintf(`{"appid":900003,"date":"2024-01-15"}`)
	}
	status, body = postJSON(t, srv, "/api/v1/games/price-context-batch",
		`{"items":[`+strings.Join(items, ",")+`]}`)
	if status != 200 || len(body["results"].([]any)) != 200 {
		t.Fatalf("应截断到 200 对: %d", len(body["results"].([]any)))
	}
}

// ── 版本聚合 ─────────────────────────────────────────────────────────────

func TestVersionsAggregation(t *testing.T) {
	srv, _, _, _ := newPortHandler(t)
	status, body := getJSON(t, srv, "/api/v1/games/900003/versions")
	if status != 200 {
		t.Fatalf("状态码 %d: %v", status, body)
	}
	versions := body["versions"].([]any)
	if len(versions) != 3 {
		t.Fatalf("bundle 行应剔除、其余 3 组: %v", versions)
	}
	first := versions[0].(map[string]any)
	if first["subId"].(float64) != 1 || first["suffix"] != nil || first["isGold"] != false {
		t.Fatalf("标准版应排首位: %v", first)
	}
	regions := first["regions"].(map[string]any)
	cn := regions["CN"].(map[string]any)
	if cn["cents"].(float64) != 5200 || cn["formatted"] != "¥52.00" || cn["discount"].(float64) != 0 || cn["cnyFen"].(float64) != 5200 {
		t.Fatalf("CN 区应为最新胜出 5200(2024-01-20): %v", cn)
	}
	us := regions["US"].(map[string]any)
	if us["cents"].(float64) != 1499 || us["formatted"] != "$14.99" {
		t.Fatalf("US 区格式异常: %v", us)
	}
	if us["cnyFen"].(float64) != 10868 {
		t.Fatalf("US cnyFen 应按汇率折算 round(1499*7.25)=10868: %v", us["cnyFen"])
	}

	// 排序：标准版 → 无后缀在售 sub → 有后缀 gold
	sub5 := versions[1].(map[string]any)
	gold := versions[2].(map[string]any)
	if sub5["subId"].(float64) != 5 || gold["subId"].(float64) != 2 {
		t.Fatalf("排序应为 标准→无后缀→gold: %v %v", sub5, gold)
	}
	if gold["isGold"] != true || gold["suffix"] != "黄金版" {
		t.Fatalf("gold 行元信息异常: %v", gold)
	}
}

func TestVersionsEmpty(t *testing.T) {
	srv, _, _, _ := newPortHandler(t)
	status, body := getJSON(t, srv, "/api/v1/games/424242/versions")
	if status != 200 {
		t.Fatalf("不存在的 appid 应 200: %d %v", status, body)
	}
	if vs, ok := body["versions"].([]any); !ok || len(vs) != 0 {
		t.Fatalf("空数据应回空列表: %v", body["versions"])
	}
}

// ── 系列成员 ─────────────────────────────────────────────────────────────

func TestSeriesMembers(t *testing.T) {
	srv, _, _, _ := newPortHandler(t)
	status, body := getJSON(t, srv, "/api/v1/games/900001/series")
	if status != 200 {
		t.Fatalf("状态码 %d: %v", status, body)
	}
	if body["seriesId"] != "onlywolf" || body["seriesName"] != "只狼" {
		t.Fatalf("系列标识/展示名异常: %v", body)
	}
	members := body["members"].([]any)
	if len(members) != 2 {
		t.Fatalf("成员应 2 名: %v", members)
	}
	self := members[0].(map[string]any)
	if self["appid"].(float64) != 900001 || self["isSelf"] != true {
		t.Fatalf("本人行应排首位: %v", self)
	}
	if self["cnPriceFen"].(float64) != 26800 || self["cnOriginalFen"].(float64) != 29800 ||
		self["cnDiscount"].(float64) != 1000 || self["lowestPriceFen"].(float64) != 43000 ||
		self["savingsFen"].(float64) != 16700 {
		t.Fatalf("本人行载荷异常: %v", self)
	}
	other := members[1].(map[string]any)
	if other["appid"].(float64) != 900004 || other["isSelf"] != false {
		t.Fatalf("次位成员异常: %v", other)
	}
	if other["cnPriceFen"] != nil || other["cnOriginalFen"] != nil ||
		other["lowestPriceFen"] != nil || other["cnDiscount"].(float64) != 0 {
		t.Fatalf("无国区行成员应为 null 对: %v", other)
	}

	// 落单与不存在 → 404
	for _, path := range []string{"/api/v1/games/900002/series", "/api/v1/games/424242/series"} {
		status, body = getJSON(t, srv, path)
		if status != 404 || body["detail"] != "未识别到同系列游戏" {
			t.Fatalf("%s 应 404: %d %v", path, status, body)
		}
	}
}

// ── 目录写三件 ───────────────────────────────────────────────────────────

func exclusionReason(t *testing.T, db *sql.DB, appid int64) (string, bool) {
	t.Helper()
	var reason sql.NullString
	var active int
	e := db.QueryRow(`SELECT reason, active FROM monitor_exclusions WHERE target_type='game' AND target_id=? AND active=1`, appid).Scan(&reason, &active)
	if e == sql.ErrNoRows {
		return "", false
	}
	if e != nil {
		t.Fatalf("排除查询失败: %v", e)
	}
	return reason.String, true
}

func TestRemoveWritesLedgerAndExclusion(t *testing.T) {
	srv, db, jobs, _ := newPortHandler(t)

	var firstRemovedAt string
	if e := db.QueryRow(`SELECT CAST(removed_at AS TEXT) FROM catalog_removals WHERE appid=900001`).Scan(&firstRemovedAt); e == nil {
		t.Fatal("夹具不应预置 900001 移除行")
	}

	status, body := postJSON(t, srv, "/api/v1/games/remove", `{"appids":[900001, 888888]}`)
	if status != 200 || body["removed"].(float64) != 1 || body["missing"].(float64) != 1 {
		t.Fatalf("首个移除计数异常: %d %v", status, body)
	}
	var reason, ledgerReason string
	if e := db.QueryRow(`SELECT reason FROM catalog_removals WHERE appid=900001`).Scan(&ledgerReason); e != nil || ledgerReason != "user_removed" {
		t.Fatalf("catalog_removals 行异常: %v %v", ledgerReason, e)
	}
	if reason, _ = exclusionReason(t, db, 900001); reason != "catalog_removed" {
		t.Fatalf("监控排除 reason 应为 catalog_removed: %q", reason)
	}
	var activeItem, excludedItem, manualItem int
	if e := db.QueryRow(`SELECT active, excluded, manual FROM wishlist_items WHERE appid=900001`).Scan(&activeItem, &excludedItem, &manualItem); e != nil ||
		activeItem != 0 || excludedItem != 1 || manualItem != 0 {
		t.Fatalf("wishlist_items 脱池异常: %d/%d/%d %v", activeItem, excludedItem, manualItem, e)
	}
	if e := db.QueryRow(`SELECT reason FROM catalog_removals WHERE appid=888888`).Scan(&ledgerReason); e == nil {
		t.Fatal("目录外 appid 不应落行")
	}

	// 幂等：重复移除保留首次时刻，removed 按目录内计
	if _, e := db.Exec(`UPDATE catalog_removals SET removed_at='2026-01-01 08:00:00' WHERE appid=900001`); e != nil {
		t.Fatal(e)
	}
	status, body = postJSON(t, srv, "/api/v1/games/remove", `{"appids":[900001, 900002]}`)
	if status != 200 || body["removed"].(float64) != 2 || body["missing"].(float64) != 0 {
		t.Fatalf("重复移除计数异常: %d %v", status, body)
	}
	if e := db.QueryRow(`SELECT CAST(removed_at AS TEXT) FROM catalog_removals WHERE appid=900001`).Scan(&firstRemovedAt); e != nil || firstRemovedAt[:10] != "2026-01-01" {
		t.Fatalf("重复移除应保留首次时刻: %v %v", firstRemovedAt, e)
	}
	var count int
	if e := db.QueryRow(`SELECT count(*) FROM catalog_removals`).Scan(&count); e != nil || count != 2 {
		t.Fatalf("账本应恰 2 行: %d %v", count, e)
	}

	// 默认视图隐藏、removed 视图独占
	_, list := getJSON(t, srv, "/api/v1/games?limit=100")
	for _, it := range list["items"].([]any) {
		if it.(map[string]any)["appid"].(float64) == 900001 {
			t.Fatal("默认视图不应出现已移除款")
		}
	}
	_, removed := getJSON(t, srv, "/api/v1/games?limit=100&removed=true")
	found := false
	for _, it := range removed["items"].([]any) {
		if it.(map[string]any)["appid"].(float64) == 900001 {
			found = true
		}
	}
	if !found {
		t.Fatal("removed 视图应独占已移除款")
	}
	if jobs.hasCall("catalog_restore") || jobs.hasCall("removed_retry") {
		t.Fatalf("移除不应触发爬取任务: %v", jobs.calls)
	}
}

func TestRestorePreservesUserExclusionAndClearsOwnReason(t *testing.T) {
	srv, db, jobs, _ := newPortHandler(t)
	// 预置：900001/900002 均已移除
	for _, appid := range []int64{900001, 900002} {
		if _, e := db.Exec(`INSERT INTO catalog_removals (appid, reason, removed_at) VALUES (?, 'user_removed', '2026-01-01 08:00:00')`, appid); e != nil {
			t.Fatal(e)
		}
	}
	if _, e := db.Exec(`INSERT INTO monitor_targets (target_type, target_id) VALUES ('game', 900001), ('game', 900002)`); e != nil {
		t.Fatal(e)
	}
	if _, e := db.Exec(`INSERT INTO monitor_exclusions (target_type, target_id, reason, active) VALUES
		('game', 900001, 'catalog_removed', 1), ('game', 900002, 'user_choice', 1)`); e != nil {
		t.Fatal(e)
	}

	// 恢复款含用户显式排除（900002）：只回商店，不洗掉排除，不进重爬
	status, body := postJSON(t, srv, "/api/v1/games/restore", `{"appids":[900001, 900002]}`)
	if status != 200 || body["restored"].(float64) != 2 || body["missing"].(float64) != 0 {
		t.Fatalf("恢复计数异常: %d %v", status, body)
	}
	var count int
	if e := db.QueryRow(`SELECT count(*) FROM catalog_removals`).Scan(&count); e != nil || count != 0 {
		t.Fatalf("恢复应删净账本行: %d %v", count, e)
	}
	if reason, ok := exclusionReason(t, db, 900002); !ok || reason != "user_choice" {
		t.Fatalf("用户显式排除应保留: %q %v", reason, ok)
	}
	if _, ok := exclusionReason(t, db, 900001); ok {
		t.Fatal("catalog_removed 排除应随恢复解除")
	}
	calls := jobs.callsOf("catalog_restore")
	if len(calls) != 1 || !strings.Contains(calls[0], "900001") || strings.Contains(calls[0], "900002") {
		t.Fatalf("重爬应只含解除排除的恢复款: %v", calls)
	}
}

func TestRestoreBusyIsLoggedNotFailed(t *testing.T) {
	srv, db, jobs, _ := newPortHandler(t)
	if _, e := db.Exec(`INSERT INTO catalog_removals (appid, reason) VALUES (900001, 'user_removed')`); e != nil {
		t.Fatal(e)
	}
	if _, e := db.Exec(`INSERT INTO monitor_targets (target_type, target_id) VALUES ('game', 900001)`); e != nil {
		t.Fatal(e)
	}
	if _, e := db.Exec(`INSERT INTO monitor_exclusions (target_type, target_id, reason, active) VALUES ('game', 900001, 'catalog_removed', 1)`); e != nil {
		t.Fatal(e)
	}
	jobs.err = errors.New("价格更新正在进行")
	status, body := postJSON(t, srv, "/api/v1/games/restore", `{"appids":[900001]}`)
	if status != 200 || body["restored"].(float64) != 1 {
		t.Fatalf("忙不应使恢复失败: %d %v", status, body)
	}
	if _, ok := exclusionReason(t, db, 900001); ok {
		t.Fatal("忙时排除解除仍应生效")
	}
}

func TestRetryRemovedContract(t *testing.T) {
	srv, db, jobs, _ := newPortHandler(t)

	status, body := postJSON(t, srv, "/api/v1/games/424242/retry-removed", "")
	if status != 404 || body["detail"] != "游戏 424242 不存在" {
		t.Fatalf("不存在游戏应 404: %d %v", status, body)
	}
	if _, e := db.Exec(`UPDATE games SET removed_at='2026-01-01 08:00:00', removed_strikes=3 WHERE appid=900001`); e != nil {
		t.Fatal(e)
	}
	status, body = postJSON(t, srv, "/api/v1/games/900001/retry-removed", "")
	if status != 200 || body["ok"] != true || body["jobId"].(float64) != 1 || body["requeued"] != true {
		t.Fatalf("复探回执异常: %d %v", status, body)
	}
	calls := jobs.callsOf("removed_retry")
	if len(calls) != 1 || !strings.Contains(calls[0], "appids|") || !strings.Contains(calls[0], "900001") {
		t.Fatalf("removed_retry 派发异常: %v", calls)
	}
	var removedAt sql.NullString
	var strikes int
	if e := db.QueryRow(`SELECT CAST(removed_at AS TEXT), removed_strikes FROM games WHERE appid=900001`).Scan(&removedAt, &strikes); e != nil ||
		removedAt.Valid || strikes != 0 {
		t.Fatalf("清标未生效: %v %d %v", removedAt, strikes, e)
	}

	// 忙 / 空任务 → note 语义不视为失败
	jobs.err = errors.New("价格更新正在进行")
	status, body = postJSON(t, srv, "/api/v1/games/900001/retry-removed", "")
	if status != 200 || body["ok"] != true || body["jobId"] != nil || body["requeued"] != false || body["note"] == "" {
		t.Fatalf("忙回执应 ok+note: %d %v", status, body)
	}
}

func TestGameIdsBodyValidation(t *testing.T) {
	srv, _, _, _ := newPortHandler(t)
	for _, tc := range []struct {
		body     string
		wantType string
	}{
		{`{}`, "missing"},
		{`{"appids": 5}`, "list_type"},
		{`{"appids": []}`, "too_short"},
		{`{"appids": ["a"]}`, "int_parsing"},
		{`{"appids": [2.5]}`, "int_from_float"},
		{`{"appids": [null]}`, "int_type"},
	} {
		status, body := postJSON(t, srv, "/api/v1/games/remove", tc.body)
		detail := body["detail"].([]any)
		if status != 422 || detail[0].(map[string]any)["type"] != tc.wantType {
			t.Fatalf("%s 应 422 %s: %d %v", tc.body, tc.wantType, status, detail)
		}
	}
	status, body := postJSON(t, srv, "/api/v1/games/remove", `{"appids":[900001], "extra":1}`)
	if status != 200 || body["removed"].(float64) != 1 {
		t.Fatalf("额外键应忽略: %d %v", status, body)
	}
}

// ── CDK 查价 ─────────────────────────────────────────────────────────────

func TestCDKFetcher(t *testing.T) {
	srv, _, _, _ := newPortHandler(t)
	hits := map[string]int{}
	var mu sync.Mutex
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		hits[r.URL.Path]++
		mu.Unlock()
		switch r.URL.Path {
		case "/steampy":
			if r.URL.Query().Get("subId") != "5" || r.URL.Query().Get("appId") != "900003" || r.URL.Query().Get("type") != "subid" {
				w.Write([]byte(`{"success":false}`))
				return
			}
			w.Write([]byte(`{"success":true,"result":{"keyPrice":12.5,"id":"abc123"}}`))
		case "/cici":
			if r.URL.Query().Get("parentId") != "900003" {
				w.Write([]byte(`{"code":500}`))
				return
			}
			w.Write([]byte(`{"code":200,"rows":[{"gameId":5,"lastLowSellPrice":8.8},{"gameId":9,"lastLowSellPrice":1}]}`))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer upstream.Close()

	oldSteampy, oldCici := cdkSteampyURL, cdkCiciURL
	cdkSteampyURL = upstream.URL + "/steampy"
	cdkCiciURL = upstream.URL + "/cici"
	t.Cleanup(func() { cdkSteampyURL, cdkCiciURL = oldSteampy, oldCici })

	// 缺省 sub：从库内解析（CN 行 sub 9 在 900001；900003 CN sub 5）
	status, body := getJSON(t, srv, "/api/v1/games/900003/cdk")
	if status != 200 {
		t.Fatalf("状态码 %d: %v", status, body)
	}
	if body["appid"].(float64) != 900003 || body["subId"].(float64) != 5 {
		t.Fatalf("sub 解析应取 CN 行 sub 5: %v", body)
	}
	steampy := body["steampy"].(map[string]any)
	if steampy["listed"] != true || steampy["price"] != "¥12.5" || steampy["url"] != "https://steampy.com/cdkDetail?name=cn&gameId=abc123" {
		t.Fatalf("steampy 载荷异常: %v", steampy)
	}
	cici := body["steamcici"].(map[string]any)
	if cici["listed"] != true || cici["price"] != "¥8.8" || cici["url"] != "https://www.steamcici.com/" {
		t.Fatalf("cici 载荷异常: %v", cici)
	}
	if _, has := steampy["error"]; has {
		t.Fatal("成功载荷不应带 error 键")
	}

	// 同键复用缓存：外部调用恰好一次
	status, body = getJSON(t, srv, "/api/v1/games/900003/cdk")
	if status != 200 || body["steampy"].(map[string]any)["price"] != "¥12.5" {
		t.Fatalf("缓存回执异常: %v", body)
	}
	mu.Lock()
	steampyHits, ciciHits := hits["/steampy"], hits["/cici"]
	mu.Unlock()
	if steampyHits != 1 || ciciHits != 1 {
		t.Fatalf("缓存未生效: steampy=%d cici=%d", steampyHits, ciciHits)
	}

	// 显式 sub 独立键：新查询、缺省键与显式键互不串
	status, body = getJSON(t, srv, "/api/v1/games/900003/cdk?sub_id=9")
	if status != 200 || body["subId"].(float64) != 9 {
		t.Fatalf("显式 sub 回执异常: %v", body)
	}
	cici = body["steamcici"].(map[string]any)
	if cici["listed"] != true || cici["price"] != "¥1" {
		t.Fatalf("显式 sub 命中另一行: %v", cici)
	}
	mu.Lock()
	totalSteampy, totalCici := hits["/steampy"], hits["/cici"]
	mu.Unlock()
	if totalSteampy != 2 || totalCici != 2 {
		t.Fatalf("显式键应触发新查询: steampy=%d cici=%d", totalSteampy, totalCici)
	}

	// 坏 sub_id → 422
	status, body = getJSON(t, srv, "/api/v1/games/900003/cdk?sub_id=abc")
	if status != 422 {
		t.Fatalf("坏 sub_id 应 422: %d %v", status, body)
	}
}

func TestCDKErrorFieldAndResolveOrder(t *testing.T) {
	srv, _, _, _ := newPortHandler(t)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/steampy":
			w.Write([]byte(`{"success":true,"result":{"keyPrice":30,"id":"id9"}}`))
		case "/cici":
			// 非 JSON 响应 → error 字段，不抛错
			w.WriteHeader(http.StatusInternalServerError)
			w.Write([]byte("oops"))
		}
	}))
	defer upstream.Close()

	oldSteampy, oldCici := cdkSteampyURL, cdkCiciURL
	cdkSteampyURL = upstream.URL + "/steampy"
	cdkCiciURL = upstream.URL + "/cici"
	t.Cleanup(func() { cdkSteampyURL, cdkCiciURL = oldSteampy, oldCici })

	// resolve 顺序：CN 优先（900001 CN sub 9 > JP sub 3）
	status, body := getJSON(t, srv, "/api/v1/games/900001/cdk")
	if status != 200 || body["subId"].(float64) != 9 {
		t.Fatalf("sub 解析应 CN 优先: %d %v", status, body)
	}
	if body["steampy"].(map[string]any)["price"] != "¥30" {
		t.Fatalf("整值价格渲染异常: %v", body["steampy"])
	}
	cici := body["steamcici"].(map[string]any)
	if cici["listed"] != false || cici["price"] != nil || cici["error"] == "" {
		t.Fatalf("上游故障应落 error 字段不抛错: %v", cici)
	}

	// 无任何价格行的游戏：sub 解析 null，双平台不发起 HTTP
	status, body = getJSON(t, srv, "/api/v1/games/900004/cdk")
	if status != 200 || body["subId"] != nil {
		t.Fatalf("无价格行应 subId null: %d %v", status, body)
	}
	if body["steampy"].(map[string]any)["listed"] != false || body["steamcici"].(map[string]any)["listed"] != false {
		t.Fatalf("无 sub 应双平台 listed false: %v", body)
	}
}

// ── 关联捆绑包包壳 ───────────────────────────────────────────────────────

func TestBundlesShell(t *testing.T) {
	srv, db, _, _ := newPortHandler(t)
	if _, e := db.Exec(`INSERT INTO bundles (bundle_id, name, must_purchase_as_set, item_kind,
		min_cny_fen, diff_fen, url, app_ids) VALUES (501, '星露Bundle', 1, 0, 3800, 200, NULL, '[900003]')`); e != nil {
		t.Fatal(e)
	}
	if _, e := db.Exec(`INSERT INTO bundle_region_prices (bundle_id, region_code, currency, price, cny_fen)
		VALUES (501, 'CN', 'CNY', 3800, 3800)`); e != nil {
		t.Fatal(e)
	}

	status, body := getJSON(t, srv, "/api/v1/games/900003/bundles")
	if status != 200 {
		t.Fatalf("状态码 %d: %v", status, body)
	}
	bundles := body["bundles"].([]any)
	if len(bundles) != 1 {
		t.Fatalf("关联捆绑包应 1 条: %v", bundles)
	}
	b := bundles[0].(map[string]any)
	if b["bundleId"].(float64) != 501 || b["priceCny"].(float64) != 3800 ||
		b["lowestPriceFen"].(float64) != 3800 || b["lowestRegion"] != "cn" {
		t.Fatalf("包壳载荷异常: %v", b)
	}
	status, body = getJSON(t, srv, "/api/v1/games/900001/bundles")
	if status != 200 || len(body["bundles"].([]any)) != 0 {
		t.Fatalf("无命中应空数组: %d %v", status, body)
	}
}
