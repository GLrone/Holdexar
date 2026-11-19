package agent

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"holdexar/server-go/internal/bundles"
	"holdexar/server-go/internal/crawl"
	"holdexar/server-go/internal/epicfree"
	"holdexar/server-go/internal/games"
	"holdexar/server-go/internal/humblechoice"
	"holdexar/server-go/internal/monitoring"
	"holdexar/server-go/internal/rates"
	"holdexar/server-go/internal/regions"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/steamevents"
	"holdexar/server-go/internal/store"
)

const catalogExtraSchema = `
DROP TABLE crawl_jobs;
CREATE TABLE crawl_jobs (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	kind TEXT,
	status TEXT,
	mode TEXT,
	cycle_id INTEGER,
	regions_json TEXT,
	stats_json TEXT,
	started_at TEXT,
	finished_at TEXT,
	error TEXT
);
ALTER TABLE bundles ADD COLUMN smart_score REAL;
ALTER TABLE game_price_history ADD COLUMN is_bundle INTEGER NOT NULL DEFAULT 0;
ALTER TABLE game_price_history ADD COLUMN product_unknown INTEGER NOT NULL DEFAULT 0;
CREATE TABLE price_events (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
	cycle_id INTEGER NOT NULL,
	appid INTEGER NOT NULL,
	region_code VARCHAR(8),
	event_type VARCHAR(32) NOT NULL,
	previous_json JSON,
	current_json JSON,
	occurred_at DATETIME,
	created_at DATETIME
);
CREATE TABLE steam_events (
	id INTEGER NOT NULL,
	event_key VARCHAR(60) NOT NULL,
	category VARCHAR(20) NOT NULL,
	name_en VARCHAR(120) NOT NULL,
	name_zh VARCHAR(120),
	start_date DATE NOT NULL,
	end_date DATE NOT NULL,
	precise_start_ts BIGINT,
	precise_end_ts BIGINT,
	store_url VARCHAR(200),
	source_updated_at DATETIME,
	PRIMARY KEY (id),
	UNIQUE (event_key)
);
CREATE TABLE tracked_accounts (
	steamid VARCHAR(20) NOT NULL,
	label VARCHAR(100),
	persona_name VARCHAR(100),
	avatar_url VARCHAR(500),
	kinds_json JSON,
	last_sync_at DATETIME,
	created_at DATETIME,
	PRIMARY KEY (steamid)
);
CREATE TABLE wishlist_items (
	steamid VARCHAR(20) NOT NULL,
	appid BIGINT NOT NULL,
	added_at DATETIME,
	active BOOLEAN NOT NULL,
	owned BOOLEAN NOT NULL,
	manual BOOLEAN NOT NULL,
	wishlisted BOOLEAN NOT NULL,
	manual_pool BOOLEAN NOT NULL,
	excluded BOOLEAN NOT NULL,
	board_pool BOOLEAN NOT NULL,
	PRIMARY KEY (steamid, appid)
);
CREATE TABLE steam_accounts (
	steam_id TEXT PRIMARY KEY,
	bound_at TEXT,
	wallet_json TEXT
);
`

func newCatalogSurface(t *testing.T, board []int64) (*http.ServeMux, *sql.DB) {
	t.Helper()
	dataDir := t.TempDir()
	db, err := store.Open(filepath.Join(dataDir, "holdexar.db"))
	if err != nil {
		t.Fatalf("打开测试库失败: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	for _, stmt := range []string{testSchema, catalogExtraSchema} {
		if _, err := db.Exec(stmt); err != nil {
			t.Fatalf("建表失败: %v", err)
		}
	}
	seedTools(t, db)

	mux := http.NewServeMux()
	gate := store.NewWriteGate(db, filepath.Join(dataDir, "holdexar.db"))
	regions.Register(mux, db)
	rates.Register(mux, db)
	games.Register(mux, db, nil, games.Options{
		BoardGet: func(context.Context, string) ([]int64, error) { return board, nil },
	})
	bundles.NewService(db, gate, monitoring.New(db, gate)).Register(mux, bundles.RegisterOptions{})
	humblechoice.New(db, gate, humblechoice.Options{}).Register(mux)
	epicfree.New(context.Background(), db, gate, epicfree.Options{}).Register(mux)
	steamevents.New(db, gate, steamevents.Options{}).Register(mux)
	crawl.New(db, gate, nil, crawl.Options{}).Register(mux, context.Background())
	return mux, db
}

func catalogRegistryOrDie(t *testing.T) *ToolRegistry {
	t.Helper()
	r := newRegistry()
	if err := RegisterCatalogPriceTools(r); err != nil {
		t.Fatalf("目录价格工具注册失败: %v", err)
	}
	return r
}

func executeCatalogTool(t *testing.T, mux *http.ServeMux, db *sql.DB, name string, argsJSON string) map[string]any {
	t.Helper()
	spec := catalogRegistryOrDie(t).get(name)
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

func seedGame900002(t *testing.T, db *sql.DB) {
	t.Helper()
	if _, err := db.Exec(`INSERT INTO games (appid, name, name_en, positive_rate, review_count) VALUES
		(900002, '传送门骑士', 'Portal Knights', 9200, 80000)`); err != nil {
		t.Fatalf("种子写入失败: %v", err)
	}
	if _, err := db.Exec(`INSERT INTO game_current_prices (appid, region_code, currency, price, discount_percent, price_status, cny_fen, attempt_outcome, updated_at) VALUES
		(900002, 'CN', 'CNY', 19800, 25, 'ok', 19800, 'success', '2026-10-30 09:00:00')`); err != nil {
		t.Fatalf("种子写入失败: %v", err)
	}
}

func seedHistory900001(t *testing.T, db *sql.DB) {
	t.Helper()
	if _, err := db.Exec(`INSERT INTO game_price_history (appid, region_code, currency, price, discount_percent, price_status, cny_fen, snapshot_at) VALUES
		(900001, 'CN', 'CNY', 3000, 0, 'ok', 3000, '2026-01-01 10:00:00'),
		(900001, 'CN', 'CNY', 1000, 80, 'ok', 1000, '2026-02-01 10:00:00'),
		(900001, 'CN', 'CNY', 2000, 0, 'ok', 2000, '2026-03-01 10:00:00')`); err != nil {
		t.Fatalf("种子写入失败: %v", err)
	}
}

func seedAccountUS(t *testing.T, db *sql.DB) {
	t.Helper()
	if _, err := db.Exec(`INSERT INTO steam_accounts (steam_id, bound_at, wallet_json) VALUES
		('76561198000000001', '2026-01-01 00:00:00', '{"region_code": "US"}')`); err != nil {
		t.Fatalf("种子写入失败: %v", err)
	}
}

func seedGame900005(t *testing.T, db *sql.DB) {
	t.Helper()
	if _, err := db.Exec(`INSERT INTO games (appid, name, name_en) VALUES (900005, '双区样本', 'Dual Region')`); err != nil {
		t.Fatalf("种子写入失败: %v", err)
	}
	if _, err := db.Exec(`INSERT INTO game_current_prices (appid, region_code, currency, price, discount_percent, price_status, cny_fen, attempt_outcome, updated_at) VALUES
		(900005, 'CN', 'CNY', 4800, 0, 'ok', 4800, 'success', '2026-10-30 09:00:00'),
		(900005, 'US', 'USD', 5200, 10, 'ok', 5200, 'success', '2026-10-30 09:00:00')`); err != nil {
		t.Fatalf("种子写入失败: %v", err)
	}
}

func seedVersions900002(t *testing.T, db *sql.DB) {
	t.Helper()
	if _, err := db.Exec(`INSERT INTO game_price_history (appid, region_code, currency, price, discount_percent, sub_id, is_gold, version_suffix, price_status, cny_fen, snapshot_at) VALUES
		(900002, 'CN', 'CNY', 19800, 25, 666, 0, 'Deluxe Edition', 'ok', 19800, '2026-10-01 10:00:00'),
		(900002, 'CN', 'CNY', 29800, 0, 777, 1, NULL, 'ok', 29800, '2026-10-01 10:00:00')`); err != nil {
		t.Fatalf("种子写入失败: %v", err)
	}
}

func seedBundleFollow(t *testing.T, db *sql.DB) {
	t.Helper()
	if _, err := db.Exec(`INSERT INTO monitor_sources (target_type, target_id, source, active) VALUES
		('bundle', 501, 'favorite', 1)`); err != nil {
		t.Fatalf("种子写入失败: %v", err)
	}
}

func TestCatalogPriceRegistry(t *testing.T) {
	r := catalogRegistryOrDie(t)
	budget := map[string]float64{
		"diagnose_price": 60.0, "top_games": 60.0, "price_drops": 60.0, "list_bundles": 60.0,
		"hb_monthly": 40.0, "epic_free": 40.0, "steam_free": 40.0, "calendar_events": 40.0,
		"rates_overview": 40.0,
	}
	if len(r.all()) != 16 {
		t.Fatalf("注册数量不符: got %d want 16", len(r.all()))
	}
	seen := map[string]bool{}
	for _, spec := range r.all() {
		if spec.Group != GroupRead {
			t.Errorf("%s 组别不符: %s", spec.Name, spec.Group)
		}
		if spec.Risk != "low" {
			t.Errorf("%s 风险级不符: %s", spec.Name, spec.Risk)
		}
		if spec.Handler == nil || spec.Description == "" || spec.Parameters == "" {
			t.Errorf("%s 契约字段缺失", spec.Name)
		}
		if seen[spec.Name] {
			t.Errorf("工具重复注册: %s", spec.Name)
		}
		seen[spec.Name] = true
		wantBudget, ok := budget[spec.Name]
		if !ok {
			wantBudget = budgetDefaultS
		}
		if spec.TimeoutS != wantBudget {
			t.Errorf("%s 预算不符: got %v want %v", spec.Name, spec.TimeoutS, wantBudget)
		}
	}
}

func TestCatalogPriceShapes(t *testing.T) {
	mux, db := newCatalogSurface(t, []int64{900001, 900002})
	seedGame900002(t, db)
	seedHistory900001(t, db)
	seedAccountUS(t, db)
	seedGame900005(t, db)
	seedVersions900002(t, db)
	seedBundleFollow(t, db)
	if _, err := db.Exec(`UPDATE bundle_region_prices SET app_ids = '[900001]' WHERE bundle_id = 501`); err != nil {
		t.Fatalf("种子写入失败: %v", err)
	}
	if _, err := db.Exec(`INSERT INTO app_settings (key, value_json) VALUES
		('hb_choice_last_month', '{"machineName":"nov_2026_choice","productName":""}')`); err != nil {
		t.Fatalf("种子写入失败: %v", err)
	}
	if _, err := db.Exec(`INSERT INTO games (appid, name, is_hb, hb_data, min_cny_fen) VALUES
		(900003, '月包游戏A', 1, '{"month": "HB慈善包26年11月包"}', 1500)`); err != nil {
		t.Fatalf("种子写入失败: %v", err)
	}
	if _, err := db.Exec(`INSERT INTO game_current_prices (appid, region_code, currency, price, original_price, discount_percent, price_status, cny_fen, attempt_outcome, updated_at) VALUES
		(900003, 'CN', 'CNY', 1888, 2888, 34, 'ok', 1888, 'success', '2026-10-30 09:00:00')`); err != nil {
		t.Fatalf("种子写入失败: %v", err)
	}
	if _, err := db.Exec(`INSERT INTO games (appid, name, free_kind, promo_end_at) VALUES
		(900004, '限时免费样', 'promo', ?)`, time.Now().Add(48*time.Hour).Unix()); err != nil {
		t.Fatalf("种子写入失败: %v", err)
	}
	if _, err := db.Exec(`INSERT INTO game_current_prices (appid, region_code, currency, price, original_price, discount_percent, price_status, cny_fen, attempt_outcome, updated_at) VALUES
		(900004, 'CN', 'CNY', 0, 9900, 100, 'ok', 0, 'success', '2026-10-30 09:00:00')`); err != nil {
		t.Fatalf("种子写入失败: %v", err)
	}
	epicNow := float64(time.Now().Unix())
	epicPayload, _ := json.Marshal(map[string]any{
		"source": "epic-offers", "ok": true,
		"offers": []map[string]any{{
			"title": "Sample Quest", "titleCn": "试玩大冒险", "appid": 900001,
			"start": "2026-11-01", "end": "2026-11-20", "upcoming": false,
			"image": "https://img/epic.png", "url": "", "priceOriginal": "US$9.99",
		}},
		"mobile": map[string]any{
			"title": "试玩大冒险", "image": "https://img/m.png", "url": "",
			"end": "2026-11-20", "worth": "US$9.99", "source": "mobile", "direct": false,
		},
		"fetchedAt": "2026-11-01T00:00:00",
	})
	if _, err := db.Exec(`INSERT INTO app_settings (key, value_json) VALUES
		('metadata.epic_offers_cache', ?)`, `{"fetched_at": `+jsonNumber(epicNow)+`, "payload": `+string(epicPayload)+`}`); err != nil {
		t.Fatalf("种子写入失败: %v", err)
	}
	now := time.Now().In(sched.DefaultLoc)
	if _, err := db.Exec(`INSERT INTO steam_events (id, event_key, category, name_en, name_zh, start_date, end_date) VALUES
		(1, 'autumn_sale', 'sale', 'Autumn Sale', '秋促', ?, ?),
		(2, 'winter_sale', 'seasonal_sale', 'Winter Sale', '冬促', ?, ?),
		(3, 'spring_sale', 'sale', 'Spring Sale', '春促', ?, ?)`,
		now.AddDate(0, 0, -1).Format("2006-01-02"), now.AddDate(0, 0, 2).Format("2006-01-02"),
		now.AddDate(0, 0, 5).Format("2006-01-02"), now.AddDate(0, 0, 15).Format("2006-01-02"),
		now.AddDate(0, 0, 3).Format("2006-01-02"), now.AddDate(0, 0, 8).Format("2006-01-02")); err != nil {
		t.Fatalf("种子写入失败: %v", err)
	}
	if _, err := db.Exec(`INSERT INTO price_events (cycle_id, appid, region_code, event_type, previous_json, current_json, occurred_at, created_at) VALUES
		(1, 900001, 'CN', 'PRICE_LOW', '{}', '{}', '2026-11-10 08:00:00', '2026-11-10 08:00:00')`); err != nil {
		t.Fatalf("种子写入失败: %v", err)
	}

	payload := executeCatalogTool(t, mux, db, "top_games", ``)
	if payload["kind"] != "games" || payload["titleKey"] != "top" {
		t.Fatalf("top_games 形状不符: %v", payload)
	}
	if payload["total"].(float64) != 2 {
		t.Fatalf("top_games total 不符: %v", payload["total"])
	}
	items := payload["items"].([]any)
	if len(items) != 2 || items[0].(map[string]any)["appid"].(float64) != 900001 {
		t.Fatalf("top_games 条目不符: %v", items)
	}
	if items[0].(map[string]any)["name"] != "星露谷物语" {
		t.Fatalf("top_games 简报缺失: %v", items[0])
	}

	payload = executeCatalogTool(t, mux, db, "get_price_briefing", `{"appid": 900001}`)
	if payload["kind"] != "price" || payload["appid"].(float64) != 900001 {
		t.Fatalf("briefing 形状不符: %v", payload)
	}
	cn := payload["cn"].(map[string]any)
	if cn["cnyFen"].(float64) != 4800 || cn["discount"].(float64) != 0 {
		t.Fatalf("briefing 国区价不符: %v", cn)
	}
	lowest := payload["lowest"].(map[string]any)
	if lowest["cnyFen"].(float64) != 1000 || lowest["snapshotAt"] == "" {
		t.Fatalf("briefing 史低不符: %v", lowest)
	}
	year := payload["year"].(map[string]any)
	if year["minFen"].(float64) != 1000 || year["maxFen"].(float64) != 3000 ||
		year["medianFen"].(float64) != 2000 || year["count"].(float64) != 3 {
		t.Fatalf("briefing 年度区间不符: %v", year)
	}
	trend := payload["trend"].([]any)
	if len(trend) != 3 || trend[0].([]any)[1].(float64) != 3000 || trend[2].([]any)[1].(float64) != 2000 {
		t.Fatalf("briefing 走势不符: %v", trend)
	}

	payload = executeCatalogTool(t, mux, db, "get_region_prices", `{"appid": 900005}`)
	if payload["kind"] != "region_prices" || payload["accountRegion"] != "us" {
		t.Fatalf("region_prices 形状不符: %v", payload)
	}
	if payload["count"].(float64) != 2 {
		t.Fatalf("region_prices 计数不符: %v", payload)
	}
	ritems := payload["items"].([]any)
	if ritems[0].(map[string]any)["region"] != "US" || ritems[1].(map[string]any)["region"] != "CN" {
		t.Fatalf("region_prices 排序不符: %v", ritems)
	}
	if ritems[0].(map[string]any)["discount"].(float64) != 10 || ritems[0].(map[string]any)["cnyFen"].(float64) != 5200 {
		t.Fatalf("region_prices 行内容不符: %v", ritems[0])
	}

	payload = executeCatalogTool(t, mux, db, "compare_games", `{"appids": [900001, 900002]}`)
	if payload["kind"] != "compare" {
		t.Fatalf("compare 形状不符: %v", payload)
	}
	citems := payload["items"].([]any)
	if len(citems) != 2 {
		t.Fatalf("compare 条目数不符: %v", citems)
	}
	first := citems[0].(map[string]any)
	if first["cnyFen"].(float64) != 4800 || first["lowestFen"].(float64) != 1000 || first["medianFen"].(float64) != 2000 {
		t.Fatalf("compare 首款事实不符: %v", first)
	}
	if first["region"] != nil {
		t.Fatalf("compare 国区可买时 region 应 null: %v", first)
	}
	if citems[1].(map[string]any)["discount"].(float64) != 25 {
		t.Fatalf("compare 次款折扣不符: %v", citems[1])
	}

	if _, err := db.Exec(`INSERT INTO game_current_prices (appid, region_code, price_status, attempt_outcome, steam_answer, last_success_at, updated_at) VALUES
		(900001, 'US', 'missing', 'failed', 'parse_failed', '2026-10-01 08:00:00', '2026-10-30 09:00:00')`); err != nil {
		t.Fatalf("种子写入失败: %v", err)
	}
	payload = executeCatalogTool(t, mux, db, "diagnose_price", `{"appid": 900001}`)
	if payload["kind"] != "rows" || payload["titleKey"] != "diagnosis" || payload["appid"].(float64) != 900001 {
		t.Fatalf("diagnose 形状不符: %v", payload)
	}
	drows := payload["rows"].([]any)
	if len(drows) != 2 {
		t.Fatalf("diagnose 行数不符: %v", drows)
	}
	cover := drows[0].(map[string]any)
	if cover["vKey"] != "coverage" || cover["tone"] != "warn" {
		t.Fatalf("diagnose 覆盖行不符: %v", cover)
	}
	cdata := cover["data"].(map[string]any)
	if cdata["ok"].(float64) != 1 || cdata["total"].(float64) != 2 {
		t.Fatalf("diagnose 覆盖计数不符: %v", cdata)
	}
	problem := drows[1].(map[string]any)
	if problem["k"] != "US" || problem["vKey"] != "failed" || problem["v"] != "parse_failed" ||
		problem["tone"] != "bad" || problem["at"] != "2026-10-01T08:00:00" {
		t.Fatalf("diagnose 问题区行不符: %v", problem)
	}

	payload = executeCatalogTool(t, mux, db, "game_price_history", `{"appid": 900001}`)
	if payload["kind"] != "price_history" || payload["region"] != "cn" || payload["days"].(float64) != 365 {
		t.Fatalf("history 形状不符: %v", payload)
	}
	if payload["count"].(float64) != 3 || payload["subId"] != nil {
		t.Fatalf("history 计数/subId 不符: %v", payload)
	}
	hpoints := payload["points"].([]any)
	if len(hpoints) != 3 || hpoints[1].([]any)[1].(float64) != 1000 {
		t.Fatalf("history 走势不符: %v", hpoints)
	}
	if payload["lowest"].(map[string]any)["cnyFen"].(float64) != 1000 ||
		payload["highest"].(map[string]any)["cnyFen"].(float64) != 3000 {
		t.Fatalf("history 极值不符: %v", payload["lowest"])
	}

	payload = executeCatalogTool(t, mux, db, "game_versions", `{"appid": 900002}`)
	if payload["kind"] != "game_versions" {
		t.Fatalf("versions 形状不符: %v", payload)
	}
	versions := payload["versions"].([]any)
	if len(versions) != 2 {
		t.Fatalf("versions 条目数不符: %v", versions)
	}
	var deluxe, goldVer map[string]any
	for _, raw := range versions {
		m := raw.(map[string]any)
		switch m["subId"].(float64) {
		case 666:
			deluxe = m
		case 777:
			goldVer = m
		}
	}
	if deluxe == nil || deluxe["suffix"] != "Deluxe Edition" || deluxe["isGold"] != false {
		t.Fatalf("versions 普通版不符: %v", deluxe)
	}
	vcn := deluxe["regions"].(map[string]any)["CN"].(map[string]any)
	if vcn["discount"].(float64) != 25 || vcn["cnyFen"].(float64) != 19800 || vcn["price"] == "" {
		t.Fatalf("versions 区价不符: %v", vcn)
	}
	if goldVer == nil || goldVer["isGold"] != true || goldVer["suffix"] != "" {
		t.Fatalf("versions Gold 版不符: %v", goldVer)
	}

	payload = executeCatalogTool(t, mux, db, "bundle_detail", `{"bundle_id": 501}`)
	if payload["kind"] != "bundle_detail" || payload["bundleId"].(float64) != 501 {
		t.Fatalf("bundle_detail 形状不符: %v", payload)
	}
	if payload["cnCnyFen"].(float64) != 4000 || payload["diffFen"].(float64) != 200 {
		t.Fatalf("bundle_detail 包价不符: %v", payload)
	}
	blow := payload["lowest"].(map[string]any)
	if blow["region"] != "us" || blow["cnyFen"].(float64) != 3800 {
		t.Fatalf("bundle_detail 最低区不符: %v", blow)
	}
	bregions := payload["regionPrices"].(map[string]any)
	if _, has := bregions["CN"].(map[string]any)["appIds"]; has {
		t.Fatal("bundle_detail 区价应剥离 appIds")
	}
	if bregions["CN"].(map[string]any)["cnyFen"].(float64) != 4000 {
		t.Fatalf("bundle_detail CN 区价不符: %v", bregions["CN"])
	}
	if payload["memberTotal"].(float64) != 1 {
		t.Fatalf("bundle_detail 成员总数不符: %v", payload["memberTotal"])
	}
	members := payload["members"].([]any)
	if len(members) != 1 || members[0].(map[string]any)["appid"].(float64) != 900001 {
		t.Fatalf("bundle_detail 成员不符: %v", members)
	}

	payload = executeCatalogTool(t, mux, db, "list_bundles", ``)
	if payload["kind"] != "rows" || payload["titleKey"] != "bundlesAll" || payload["total"].(float64) != 1 {
		t.Fatalf("list_bundles 形状不符: %v", payload)
	}
	lrows := payload["rows"].([]any)
	if lrows[0].(map[string]any)["k"] != "星露Bundle" || lrows[0].(map[string]any)["v"] != "US" {
		t.Fatalf("list_bundles 行不符: %v", lrows[0])
	}
	ldata := lrows[0].(map[string]any)["data"].(map[string]any)
	if ldata["priceFen"].(float64) != 4000 || ldata["lowFen"].(float64) != 3800 || ldata["bundleId"].(float64) != 501 {
		t.Fatalf("list_bundles data 不符: %v", ldata)
	}

	payload = executeCatalogTool(t, mux, db, "list_bundle_follows", ``)
	if payload["total"].(float64) != 1 {
		t.Fatalf("list_bundle_follows total 不符: %v", payload)
	}
	if payload["rows"].([]any)[0].(map[string]any)["k"] != "星露Bundle" {
		t.Fatalf("list_bundle_follows 行不符: %v", payload["rows"])
	}

	payload = executeCatalogTool(t, mux, db, "rates_overview", ``)
	if payload["kind"] != "rows" || payload["titleKey"] != "rates" || payload["total"].(float64) != 2 {
		t.Fatalf("rates_overview 形状不符: %v", payload)
	}
	rrow := payload["rows"].([]any)[0].(map[string]any)
	if rrow["k"] != "JPY" || rrow["vKey"] != "rateLine" {
		t.Fatalf("rates_overview 行不符: %v", rrow)
	}
	if rrow["data"].(map[string]any)["rate"].(float64) != 0.048 {
		t.Fatalf("rates_overview 汇率不符: %v", rrow["data"])
	}

	payload = executeCatalogTool(t, mux, db, "hb_monthly", ``)
	if payload["kind"] != "rows" || payload["titleKey"] != "hb" || payload["total"].(float64) != 1 {
		t.Fatalf("hb_monthly 形状不符: %v", payload)
	}
	hrow := payload["rows"].([]any)[0].(map[string]any)
	if hrow["k"] != "月包游戏A" || hrow["vKey"] != "offerDiscount" || hrow["v"] != "-34%" {
		t.Fatalf("hb_monthly 行不符: %v", hrow)
	}
	hdata := hrow["data"].(map[string]any)
	if hdata["priceFen"].(float64) != 1888 || hdata["lowFen"].(float64) != 1500 {
		t.Fatalf("hb_monthly data 不符: %v", hdata)
	}

	payload = executeCatalogTool(t, mux, db, "epic_free", ``)
	if payload["kind"] != "epic" || payload["titleKey"] != "epic" || payload["total"].(float64) != 1 {
		t.Fatalf("epic_free 形状不符: %v", payload)
	}
	eoffers := payload["offers"].([]any)
	eo := eoffers[0].(map[string]any)
	if eo["titleCn"] != "试玩大冒险" || eo["upcoming"] != false || eo["url"] != "https://store.epicgames.com/en-US/free-games" {
		t.Fatalf("epic_free 清单不符: %v", eo)
	}
	if payload["fetchedAt"] != "2026-11-01T00:00:00" {
		t.Fatalf("epic_free fetchedAt 不符: %v", payload["fetchedAt"])
	}
	erows := payload["rows"].([]any)
	if erows[0].(map[string]any)["vKey"] != "epicFree" || erows[0].(map[string]any)["tone"] != "ok" {
		t.Fatalf("epic_free 行不符: %v", erows[0])
	}
	emobile := payload["mobile"].(map[string]any)
	if emobile["title"] != "试玩大冒险" || emobile["direct"] != false ||
		emobile["url"] != "https://store.epicgames.com/mobile" {
		t.Fatalf("epic_free 移动端不符: %v", emobile)
	}

	payload = executeCatalogTool(t, mux, db, "steam_free", ``)
	if payload["kind"] != "rows" || payload["titleKey"] != "steamFree" || payload["total"].(float64) != 1 {
		t.Fatalf("steam_free 形状不符: %v", payload)
	}
	srow := payload["rows"].([]any)[0].(map[string]any)
	if srow["k"] != "限时免费样" || srow["vKey"] != "offerPrice" {
		t.Fatalf("steam_free 行不符: %v", srow)
	}

	payload = executeCatalogTool(t, mux, db, "price_drops", ``)
	if payload["kind"] != "games" || payload["titleKey"] != "drops" || payload["total"].(float64) != 1 {
		t.Fatalf("price_drops 形状不符: %v", payload)
	}
	ditem := payload["items"].([]any)[0].(map[string]any)
	if ditem["name"] != "星露谷物语" || ditem["appid"].(float64) != 900001 {
		t.Fatalf("price_drops 简报不符: %v", ditem)
	}
	note := ditem["note"].(map[string]any)
	if note["key"] != "ev_price_low" || note["v"] != "CN" || note["at"] != "2026-11-10T08:00:00" {
		t.Fatalf("price_drops 注记不符: %v", note)
	}

	payload = executeCatalogTool(t, mux, db, "calendar_events", ``)
	if payload["kind"] != "rows" || payload["titleKey"] != "calendar" || payload["total"].(float64) != 3 {
		t.Fatalf("calendar_events 形状不符: %v", payload)
	}
	crows := payload["rows"].([]any)
	if crows[0].(map[string]any)["vKey"] != "eventLive" || crows[0].(map[string]any)["tone"] != "ok" {
		t.Fatalf("calendar_events 进行中行不符: %v", crows[0])
	}
	if !strings.Contains(crows[0].(map[string]any)["v"].(string), "~") {
		t.Fatalf("calendar_events 进行中区间不符: %v", crows[0])
	}
	if crows[1].(map[string]any)["k"] != "冬促" || crows[1].(map[string]any)["vKey"] != "eventUpcoming" {
		t.Fatalf("calendar_events 季节大促置顶不符: %v", crows[1])
	}
	if crows[2].(map[string]any)["k"] != "春促" {
		t.Fatalf("calendar_events 其余预告序不符: %v", crows[2])
	}
}

func TestCatalogPriceEmpty(t *testing.T) {
	mux, db := newCatalogSurface(t, nil)

	payload := executeCatalogTool(t, mux, db, "top_games", ``)
	if payload["kind"] != "empty" || payload["note"] != "no_data" {
		t.Fatalf("top_games 空态不符: %v", payload)
	}

	payload = executeCatalogTool(t, mux, db, "get_price_briefing", `{"appid": 42}`)
	if payload["kind"] != "empty" || payload["note"] != "no_data" {
		t.Fatalf("briefing 空态不符: %v", payload)
	}
	payload = executeCatalogTool(t, mux, db, "get_region_prices", `{"appid": 42}`)
	if payload["kind"] != "empty" || payload["note"] != "no_game" {
		t.Fatalf("region_prices 空态不符: %v", payload)
	}
	payload = executeCatalogTool(t, mux, db, "game_price_history", `{"appid": 900001, "region": "ru"}`)
	if payload["kind"] != "empty" || payload["note"] != "no_data" || payload["region"] != "ru" {
		t.Fatalf("history 空态不符: %v", payload)
	}
	payload = executeCatalogTool(t, mux, db, "game_versions", `{"appid": 900001}`)
	if payload["kind"] != "empty" || payload["note"] != "no_data" {
		t.Fatalf("versions 空态不符: %v", payload)
	}
	payload = executeCatalogTool(t, mux, db, "bundle_detail", `{"bundle_id": 999}`)
	if payload["kind"] != "empty" || payload["note"] != "no_bundle" {
		t.Fatalf("bundle_detail 空态不符: %v", payload)
	}

	payload = executeCatalogTool(t, mux, db, "compare_games", `{"appids": [900001]}`)
	if payload["kind"] != "empty" || payload["note"] != "need_two" {
		t.Fatalf("compare need_two 不符: %v", payload)
	}
	payload = executeCatalogTool(t, mux, db, "compare_games", `{"appids": [42, 43]}`)
	if payload["kind"] != "empty" || payload["note"] != "no_data" {
		t.Fatalf("compare no_data 不符: %v", payload)
	}

	payload = executeCatalogTool(t, mux, db, "diagnose_price", `{"appid": 42}`)
	if payload["kind"] != "empty" || payload["note"] != "no_data" {
		t.Fatalf("diagnose 空态不符: %v", payload)
	}
	payload = executeCatalogTool(t, mux, db, "diagnose_price", `{"appid": 0}`)
	if payload["kind"] != "empty" || payload["note"] != "need_appid" {
		t.Fatalf("diagnose need_appid 不符: %v", payload)
	}

	if _, err := db.Exec(`DELETE FROM bundles`); err != nil {
		t.Fatalf("清空包档失败: %v", err)
	}
	payload = executeCatalogTool(t, mux, db, "list_bundles", ``)
	if payload["kind"] != "rows" || payload["total"].(float64) != 0 {
		t.Fatalf("list_bundles 空态不符: %v", payload)
	}
	if rows, ok := payload["rows"].([]any); !ok || len(rows) != 0 {
		t.Fatalf("list_bundles 空行卡不符: %v", payload["rows"])
	}

	payload = executeCatalogTool(t, mux, db, "list_bundle_follows", ``)
	if payload["kind"] != "rows" || payload["total"].(float64) != 0 {
		t.Fatalf("list_bundle_follows 空态不符: %v", payload)
	}

	if _, err := db.Exec(`DELETE FROM fx_rates`); err != nil {
		t.Fatalf("清空汇率失败: %v", err)
	}
	payload = executeCatalogTool(t, mux, db, "rates_overview", ``)
	if payload["kind"] != "rows" || payload["total"].(float64) != 0 {
		t.Fatalf("rates_overview 空态不符: %v", payload)
	}

	payload = executeCatalogTool(t, mux, db, "hb_monthly", ``)
	if payload["kind"] != "rows" || payload["total"].(float64) != 0 {
		t.Fatalf("hb_monthly 空态不符: %v", payload)
	}

	if _, err := db.Exec(`INSERT INTO app_settings (key, value_json) VALUES
		('fetch.epic_free', 'false')`); err != nil {
		t.Fatalf("种子写入失败: %v", err)
	}
	payload = executeCatalogTool(t, mux, db, "epic_free", ``)
	if payload["kind"] != "epic" || payload["total"].(float64) != 0 {
		t.Fatalf("epic_free 空态不符: %v", payload)
	}
	if offers, ok := payload["offers"].([]any); !ok || len(offers) != 0 {
		t.Fatalf("epic_free 空清单不符: %v", payload["offers"])
	}
	if payload["mobile"] != nil {
		t.Fatalf("epic_free 空态 mobile 应 null: %v", payload["mobile"])
	}

	payload = executeCatalogTool(t, mux, db, "steam_free", ``)
	if payload["kind"] != "rows" || payload["total"].(float64) != 0 {
		t.Fatalf("steam_free 空态不符: %v", payload)
	}

	payload = executeCatalogTool(t, mux, db, "price_drops", ``)
	if payload["kind"] != "games" || payload["total"].(float64) != 0 {
		t.Fatalf("price_drops 空态不符: %v", payload)
	}

	payload = executeCatalogTool(t, mux, db, "calendar_events", ``)
	if payload["kind"] != "rows" || payload["total"].(float64) != 0 {
		t.Fatalf("calendar_events 空态不符: %v", payload)
	}
}

func TestCatalogPriceDiagnoseJobs(t *testing.T) {
	mux, db := newCatalogSurface(t, nil)
	if _, err := db.Exec(`INSERT INTO crawl_jobs (kind, status, mode, cycle_id, regions_json, stats_json, started_at, finished_at, error) VALUES
		('catalog', 'failed', 'auto', NULL, '[]', '{}', '2026-11-12 07:00:00', '2026-11-12 07:01:00', '网络不可达'),
		('pool', 'ok', 'auto', 1, '[]', '{}', '2026-11-12 08:00:00', '2026-11-12 08:01:00', NULL)`); err != nil {
		t.Fatalf("种子写入失败: %v", err)
	}
	payload := executeCatalogTool(t, mux, db, "diagnose_price", `{"appid": 0}`)
	if payload["kind"] != "rows" || payload["titleKey"] != "jobs" || payload["total"].(float64) != 1 {
		t.Fatalf("diagnose 失败任务形状不符: %v", payload)
	}
	row := payload["rows"].([]any)[0].(map[string]any)
	if row["k"] != "#1 catalog" || row["v"] != "网络不可达" || row["tone"] != "bad" ||
		row["at"] != "2026-11-12T07:00:00" {
		t.Fatalf("diagnose 失败任务行不符: %v", row)
	}
}

func jsonNumber(f float64) string {
	b, _ := json.Marshal(f)
	return string(b)
}
