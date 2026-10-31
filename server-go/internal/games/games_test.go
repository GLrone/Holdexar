package games

import (
	"context"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

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
`

func newTestHandler(t *testing.T) (*httptest.Server, *sql.DB) {
	t.Helper()
	db, err := store.Open(filepath.Join(t.TempDir(), "test.db"))
	if err != nil {
		t.Fatalf("打开测试库失败: %v", err)
	}
	t.Cleanup(func() { db.Close() })

	for _, stmt := range []string{testSchema} {
		if _, err := db.Exec(stmt); err != nil {
			t.Fatalf("建表失败: %v", err)
		}
	}
	seed(t, db)

	mux := http.NewServeMux()
	Register(mux, db)
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv, db
}

func seed(t *testing.T, db *sql.DB) {
	t.Helper()
	ins := func(q string, args ...any) {
		t.Helper()
		if _, err := db.Exec(q, args...); err != nil {
			t.Fatalf("种子写入失败: %v", err)
		}
	}
	ins(`INSERT INTO games (appid, name, name_en, type, release_date, positive_rate, positive_reviews,
		review_count, hl_flag, min_cny_fen, diff_fen, updated_at) VALUES
		(900001, '只狼：影逝二度', 'Sekiro: Shadows Die Twice', 'GAME', '2019-03-22', 9600, 450000, 500000, 2, 43000, 16700, '2026-10-30 10:00:00'),
		(900002, 'Hollow Knight', '空洞骑士', 'GAME', '2017-02-24', 9700, 300000, 320000, 0, NULL, 0, '2026-10-30 10:00:00'),
		(900003, '星露谷物语', 'Stardew Valley', 'GAME', '2016-02-26', 9800, 220000, 250000, 0, 2918, 1882, '2026-10-30 10:00:00'),
		(900004, '只狼外传', NULL, 'GAME', '2020-01-01', 8000, 1000, 1200, 0, NULL, 0, '2026-10-30 10:00:00')`)

	ins(`INSERT INTO game_current_prices (appid, region_code, currency, price, discount_percent,
		price_status, cny_fen, attempt_outcome, last_success_at, updated_at) VALUES
		(900001, 'CN', 'CNY', 26800, 0, 'ok', 26800, 'success', '2026-10-30 09:00:00', '2026-10-30 09:00:00'),
		(900001, 'US', 'USD', 5999, 0, 'ok', 43493, 'success', '2026-10-30 09:00:00', '2026-10-30 09:00:00'),
		(900002, 'CN', 'CNY', 4800, 0, 'ok', 4800, 'success', '2026-10-30 09:00:00', '2026-10-30 09:00:00'),
		(900003, 'CN', 'CNY', 4800, 0, 'ok', 4800, 'success', '2026-10-30 09:00:00', '2026-10-30 09:00:00'),
		(900003, 'JP', 'JPY', 60800, 0, 'ok', NULL, 'success', '2026-10-30 09:00:00', '2026-10-30 09:00:00'),
		(900003, 'US', 'USD', NULL, 0, 'missing', NULL, 'failed', NULL, '2026-10-30 09:00:00'),
		(900004, 'CN', 'CNY', 19800, 0, 'ok', 19800, 'success', '2026-10-30 09:00:00', '2026-10-30 09:00:00')`)

	ins(`INSERT INTO catalog_removals (appid, reason) VALUES (900004, 'user')`)

	// 影子表经 UDF 回填（与触发器同源）
	ins(`INSERT INTO games_name_fts (appid, name_bi) SELECT appid, name_bigram(name, name_en) FROM games`)

	ins(`INSERT INTO crawl_regions (code, name, currency, enabled, sort) VALUES
		('cn', '中国', 'CNY', 1, 0), ('us', '美国', 'USD', 1, 1), ('jp', '日本', 'JPY', 1, 2)`)
	ins(`INSERT INTO fx_rates (currency_code, rate_to_cny) VALUES ('JPY', 0.048), ('USD', 7.25)`)

	ins(`INSERT INTO game_tags (appid, tagid, weight) VALUES
		(900003, 100, 500), (900003, 200, 300), (900003, 300, 100)`)
	ins(`INSERT INTO tags (tagid, name_zh, name_en) VALUES
		(100, '动作', 'Action'), (200, NULL, 'Souls-like'), (300, NULL, NULL)`)

	ins(`INSERT INTO game_price_history (appid, region_code, version_suffix, is_gold, sub_id) VALUES
		(900003, 'cn', '标准版', 0, 1), (900003, 'cn', '黄金版', 1, 2), (900003, 'us', '标准版', 0, 1)`)

	ins(`INSERT INTO bundles (bundle_id, name, must_purchase_as_set, item_kind, min_cny_fen, diff_fen, app_ids)
		VALUES (501, '星露Bundle', 1, 0, 3800, 200, '[900003]')`)
	ins(`INSERT INTO bundle_region_prices (bundle_id, region_code, currency, price, cny_fen) VALUES
		(501, 'CN', 'CNY', 4000, 4000), (501, 'US', 'USD', 3800, 3800)`)
}

func getJSON(t *testing.T, srv *httptest.Server, path string) (int, map[string]any) {
	t.Helper()
	resp, err := http.Get(srv.URL + path)
	if err != nil {
		t.Fatalf("请求失败 %s: %v", path, err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("响应读取失败: %v", err)
	}
	var m map[string]any
	if err := json.Unmarshal(body, &m); err != nil {
		t.Fatalf("响应解析失败 %s: %v", body, err)
	}
	return resp.StatusCode, m
}

// TestSearchCatalogFTS 两字中文与英文查询走 FTS 通道命中。
func TestSearchCatalogFTS(t *testing.T) {
	_, db := newTestHandler(t)
	ctx := context.Background()

	for _, tc := range []struct {
		term  string
		appid int64
	}{
		{"只狼", 900001},
		{"影逝二度", 900001},
		{"sekiro", 900001},
		{"hollow", 900002},
		{"星露", 900003},
		{"stardew", 900003},
	} {
		out, err := SearchCatalog(ctx, db, tc.term, 5)
		if err != nil {
			t.Fatalf("检索 %q 失败: %v", tc.term, err)
		}
		if len(out) != 1 || out[0]["appid"].(int64) != tc.appid {
			t.Fatalf("检索 %q 命中异常: %v", tc.term, out)
		}
	}
}

// TestSearchCatalogLikeFallback 单字 / 纯标点查询回退 LIKE 通道；已移除款不出现。
func TestSearchCatalogLikeFallback(t *testing.T) {
	_, db := newTestHandler(t)
	ctx := context.Background()

	out, err := SearchCatalog(ctx, db, "狼", 5)
	if err != nil {
		t.Fatalf("检索单字失败: %v", err)
	}
	if len(out) != 1 || out[0]["appid"].(int64) != 900001 {
		t.Fatalf("LIKE 兜底命中异常: %v", out)
	}

	out, err = SearchCatalog(ctx, db, "！！！", 5)
	if err != nil {
		t.Fatalf("纯标点检索失败: %v", err)
	}
	if len(out) != 0 {
		t.Fatalf("纯标点应无命中: %v", out)
	}

	if out, _ = SearchCatalog(ctx, db, "只狼", 0); len(out) != 0 {
		t.Fatalf("limit=0 应返回空: %v", out)
	}
}

// TestSearchCatalogShape 检索行形状：basePriceFen 取国区 cny_fen 快照，positiveRate 0-1。
func TestSearchCatalogShape(t *testing.T) {
	_, db := newTestHandler(t)
	ctx := context.Background()
	out, err := SearchCatalog(ctx, db, "只狼", 5)
	if err != nil || len(out) != 1 {
		t.Fatalf("检索失败: %v %v", out, err)
	}
	row := out[0]
	base, ok := row["basePriceFen"].(*int64)
	if !ok || base == nil || *base != 26800 {
		t.Fatalf("basePriceFen 应为 cny_fen 26800: %v", row["basePriceFen"])
	}
	rate, ok := row["positiveRate"].(*float64)
	if !ok || rate == nil || *rate != 96.0 {
		t.Fatalf("positiveRate 应为 0-1 口径 96: %v", row["positiveRate"])
	}
	if row["discount"].(int64) != 0 || row["reviewCount"].(int64) != 500000 {
		t.Fatalf("discount/reviewCount 异常: %v", row)
	}
	if _, ok := row["nameEn"]; !ok {
		t.Fatal("nameEn 键必须存在")
	}
}

// TestListDefault 默认排序（史低优先 → 差价梯队）、移除款隐藏、覆盖与矩阵形状。
func TestListDefault(t *testing.T) {
	srv, _ := newTestHandler(t)
	status, body := getJSON(t, srv, "/api/v1/games")
	if status != 200 {
		t.Fatalf("状态码 %d: %v", status, body)
	}
	if body["total"].(float64) != 3 || body["hasMore"].(bool) != false {
		t.Fatalf("total/hasMore 异常: %v", body)
	}
	if body["nextCursor"] != nil {
		t.Fatalf("无更多时 nextCursor 应为 null: %v", body["nextCursor"])
	}
	items, ok := body["items"].([]any)
	if !ok || len(items) != 3 {
		t.Fatalf("items 异常: %v", body["items"])
	}
	// 默认排序：史低(900001) → 差价梯队高者(900003) → (900002)
	wantOrder := []float64{900001, 900003, 900002}
	for i, it := range items {
		if it.(map[string]any)["appid"].(float64) != wantOrder[i] {
			t.Fatalf("排序第 %d 位应为 %v: %v", i, wantOrder[i], it)
		}
	}

	first := items[0].(map[string]any)
	if first["basePriceFen"].(float64) != 26800 || first["savingsFen"].(float64) != 16700 {
		t.Fatalf("首项价格字段异常: %v", first)
	}
	if first["discountLabel"].(string) != "" {
		t.Fatalf("无折扣时 discountLabel 应为空串: %v", first["discountLabel"])
	}
	matrix, ok := first["priceMatrix"].(map[string]any)
	if !ok {
		t.Fatalf("priceMatrix 缺失: %v", first)
	}
	cn, ok := matrix["CN"].([]any)
	if !ok || len(cn) != 5 || cn[0].(string) != "¥268.00" || cn[1].(float64) != 26800 ||
		cn[2].(float64) != 26800 || cn[3].(float64) != 0 || cn[4].(bool) != false {
		t.Fatalf("CN 矩阵元组异常: %v", cn)
	}
	if _, ok := matrix["US"].([]any); !ok {
		t.Fatalf("US 矩阵缺失: %v", matrix)
	}
	unavail, ok := first["unavailableRegions"].([]any)
	if !ok || len(unavail) != 0 {
		t.Fatalf("unavailableRegions 应为空数组: %v", first["unavailableRegions"])
	}
	pd, ok := first["priceData"].(map[string]any)
	if !ok {
		t.Fatalf("priceData 缺失: %v", first)
	}
	cov, ok := pd["coverage"].(map[string]any)
	if !ok {
		t.Fatalf("coverage 缺失: %v", pd)
	}
	if cov["expectedUnits"].(float64) != 3 || cov["success"].(float64) != 2 ||
		cov["notAttempted"].(float64) != 1 || cov["coverage"].(float64) != 0.6667 {
		t.Fatalf("覆盖数值异常: %v", cov)
	}
	if pd["observedAt"].(string) != "2026-10-30 09:00:00" {
		t.Fatalf("observedAt 应透传库内文本: %v", pd["observedAt"])
	}
	third := items[2].(map[string]any)
	pd3 := third["priceData"].(map[string]any)
	if _, ok := pd3["coverage"]; !ok {
		t.Fatal("coverage 键必须存在（启用区非空时为数值对象）")
	}
	if _, ok := pd3["regions"]; !ok {
		t.Fatal("priceData.regions 键必须存在")
	}
}

// TestListPagination 分页边界：hasMore 探测、游标续页、坏游标回零。
func TestListPagination(t *testing.T) {
	srv, _ := newTestHandler(t)
	status, p1 := getJSON(t, srv, "/api/v1/games?limit=2")
	if status != 200 || p1["hasMore"].(bool) != true {
		t.Fatalf("第一页 hasMore 异常: %v", p1)
	}
	cursor, ok := p1["nextCursor"].(string)
	if !ok || cursor == "" {
		t.Fatalf("第一页应有游标: %v", p1["nextCursor"])
	}
	raw, err := base64.URLEncoding.DecodeString(cursor)
	if err != nil || string(raw) != `{"offset": 2}` {
		t.Fatalf("游标应与 Python 格式字节兼容: %s (%v)", raw, err)
	}

	status, p2 := getJSON(t, srv, "/api/v1/games?limit=2&after="+cursor)
	if status != 200 {
		t.Fatalf("第二页状态码 %d", status)
	}
	items, ok := p2["items"].([]any)
	if !ok || len(items) != 1 {
		t.Fatalf("第二页应剩 1 条: %v", p2["items"])
	}
	if p2["hasMore"].(bool) != false || p2["nextCursor"] != nil {
		t.Fatalf("第二页应为末页: hasMore=%v nextCursor=%v", p2["hasMore"], p2["nextCursor"])
	}
	if items[0].(map[string]any)["appid"].(float64) != 900002 {
		t.Fatalf("第二页应为排序末位: %v", items[0])
	}

	_, p3 := getJSON(t, srv, "/api/v1/games?limit=2&after=%21%21%21")
	if p3["total"].(float64) != 3 {
		t.Fatalf("坏游标应从 0 起: %v", p3)
	}
}

// TestListQ 列表 q 过滤走 name/name_en LIKE。
func TestListQ(t *testing.T) {
	srv, _ := newTestHandler(t)
	_, body := getJSON(t, srv, "/api/v1/games?q=Sekiro")
	items := body["items"].([]any)
	if len(items) != 1 || items[0].(map[string]any)["appid"].(float64) != 900001 {
		t.Fatalf("q=Sekiro 应只命中 900001: %v", items)
	}
	_, body = getJSON(t, srv, "/api/v1/games?removed=true")
	items = body["items"].([]any)
	if len(items) != 1 || items[0].(map[string]any)["appid"].(float64) != 900004 {
		t.Fatalf("removed=true 应只出已移除款: %v", items)
	}
}

// TestListParamValidation 形状越界回 400。
func TestListParamValidation(t *testing.T) {
	srv, _ := newTestHandler(t)
	for _, path := range []string{
		"/api/v1/games?limit=0",
		"/api/v1/games?limit=101",
		"/api/v1/games?flag=bogus",
		"/api/v1/games?minRating=101",
	} {
		status, body := getJSON(t, srv, path)
		if status != 400 {
			t.Fatalf("%s 应 400: %d %v", path, status, body)
		}
	}
}

// TestDetail 详情形状：矩阵、汇率折算、覆盖、标签、版本、捆绑包。
func TestDetail(t *testing.T) {
	srv, _ := newTestHandler(t)
	status, d := getJSON(t, srv, "/api/v1/games/900003")
	if status != 200 {
		t.Fatalf("状态码 %d: %v", status, d)
	}
	if d["name"].(string) != "星露谷物语" || d["type"].(string) != "GAME" {
		t.Fatalf("基础字段异常: %v", d)
	}
	matrix := d["priceMatrix"].(map[string]any)
	jp, ok := matrix["JP"].([]any)
	if !ok || len(jp) != 4 || jp[0].(string) != "¥608" || jp[1].(float64) != 2918 ||
		jp[2].(float64) != 60800 || jp[3].(float64) != 0 {
		t.Fatalf("JP 矩阵（0 位小数币种 + 汇率折算）异常: %v", jp)
	}
	if _, ok := matrix["US"]; ok {
		t.Fatalf("missing 区不得进矩阵: %v", matrix)
	}
	unavail := d["unavailableRegions"].([]any)
	if len(unavail) != 1 || unavail[0].(string) != "US" {
		t.Fatalf("unavailableRegions 应为 [US]: %v", unavail)
	}
	if d["cnPriceCents"].(float64) != 4800 || d["cnCnyFen"].(float64) != 4800 {
		t.Fatalf("国区字段异常: %v", d)
	}
	if d["lowestCnyFen"].(float64) != 2918 || d["lowestRegionCode"].(string) != "jp" {
		t.Fatalf("最低区应为 jp: %v %v", d["lowestCnyFen"], d["lowestRegionCode"])
	}
	if d["savingsFen"].(float64) != 1882 || d["storeDataMissing"].(bool) != false {
		t.Fatalf("savingsFen/storeDataMissing 异常: %v", d)
	}

	tags := d["tags"].([]any)
	if len(tags) != 2 {
		t.Fatalf("应只留有名字的标签: %v", tags)
	}
	t0 := tags[0].(map[string]any)
	if t0["tagid"].(float64) != 100 || t0["name"].(string) != "动作" || t0["nameEn"].(string) != "Action" {
		t.Fatalf("标签 100 异常: %v", t0)
	}
	t1 := tags[1].(map[string]any)
	if t1["tagid"].(float64) != 200 || t1["name"] != nil || t1["nameEn"].(string) != "Souls-like" {
		t.Fatalf("标签 200（中文名缺失）异常: %v", t1)
	}

	versions := d["versions"].([]any)
	if len(versions) != 2 {
		t.Fatalf("版本应去重为 2: %v", versions)
	}
	goldSeen := false
	for _, v := range versions {
		vm := v.(map[string]any)
		if vm["suffix"].(string) == "黄金版" {
			goldSeen = vm["isGold"].(bool) && vm["subId"].(float64) == 2
		}
	}
	if !goldSeen {
		t.Fatalf("黄金版版本行异常: %v", versions)
	}

	bundles := d["linkedBundles"].([]any)
	if len(bundles) != 1 {
		t.Fatalf("关联捆绑包异常: %v", bundles)
	}
	b := bundles[0].(map[string]any)
	if b["bundleId"].(float64) != 501 || b["priceCny"].(float64) != 4000 ||
		b["lowestPriceFen"].(float64) != 3800 || b["lowestRegion"].(string) != "us" ||
		b["itemKind"].(float64) != 0 || b["mustPurchaseAsSet"].(float64) != 1 ||
		b["url"].(string) != "https://store.steampowered.com/bundle/501/" {
		t.Fatalf("捆绑包字段异常: %v", b)
	}
}

// TestDetailNotFound 404 文案与线上一致。
func TestDetailNotFound(t *testing.T) {
	srv, _ := newTestHandler(t)
	status, d := getJSON(t, srv, "/api/v1/games/424242")
	if status != 404 {
		t.Fatalf("状态码 %d", status)
	}
	if d["detail"] != "游戏不存在" {
		t.Fatalf("404 文案不符: %v", d)
	}
}
