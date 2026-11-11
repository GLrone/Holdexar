package dbwriter

// 写路径语义单元测试：重放保 ID、失败章保旧价、缺价穷尽转终态、
// 免费态分组、事务回滚不落半批。建库用与现网逐字一致的表 DDL。

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
)

var testDDL = []string{
	`CREATE TABLE games (
	appid BIGINT NOT NULL,
	name VARCHAR(512) NOT NULL,
	name_en VARCHAR(512),
	type VARCHAR(20),
	header_image VARCHAR(1024),
	store_url VARCHAR(1024),
	chinese_support VARCHAR(50),
	family_sharing BOOLEAN NOT NULL,
	trading_cards BOOLEAN NOT NULL,
	xgp_tier VARCHAR(50),
	is_epic BOOLEAN NOT NULL,
	epic_date VARCHAR(100),
	is_hb BOOLEAN NOT NULL,
	hb_data VARCHAR(100),
	bundle_count INTEGER,
	series_id VARCHAR(100),
	hl_flag INTEGER NOT NULL,
	pp_flag INTEGER NOT NULL,
	pp_changed_at DATETIME,
	min_cny_fen BIGINT,
	diff_fen INTEGER NOT NULL,
	smart_score FLOAT,
	is_adult BOOLEAN NOT NULL,
	is_visual_novel BOOLEAN NOT NULL,
	release_date VARCHAR(50),
	removed_at DATETIME,
	removed_strikes INTEGER NOT NULL,
	free_kind VARCHAR(10),
	promo_end_at BIGINT,
	developers JSON,
	publishers JSON,
	positive_rate INTEGER,
	positive_reviews INTEGER NOT NULL,
	review_count INTEGER NOT NULL,
	view_count INTEGER NOT NULL,
	created_at DATETIME,
	updated_at DATETIME,
	PRIMARY KEY (appid)
)`,
	`CREATE TABLE game_current_prices (
	appid BIGINT NOT NULL,
	region_code VARCHAR(10) NOT NULL,
	currency VARCHAR(10),
	price BIGINT,
	original_price BIGINT,
	discount_percent INTEGER NOT NULL,
	sub_id INTEGER,
	price_status VARCHAR(20) NOT NULL,
	fail_count INTEGER NOT NULL,
	cny_fen BIGINT,
	discount_end_ts INTEGER,
	attempt_outcome VARCHAR(10),
	steam_answer VARCHAR(12),
	last_success_at DATETIME,
	updated_at DATETIME,
	PRIMARY KEY (appid, region_code)
)`,
	`CREATE TABLE game_price_history (
	id INTEGER NOT NULL,
	appid BIGINT NOT NULL,
	region_code VARCHAR(10) NOT NULL,
	currency VARCHAR(10),
	price BIGINT,
	original_price BIGINT,
	discount_percent INTEGER NOT NULL,
	sub_id INTEGER,
	is_gold BOOLEAN NOT NULL,
	version_suffix VARCHAR(100),
	is_bundle BOOLEAN NOT NULL,
	price_status VARCHAR(20) NOT NULL,
	cny_fen BIGINT,
	discount_end_ts INTEGER,
	discount_desc VARCHAR(60),
	bundle_id INTEGER,
	bundle_discount_pct INTEGER,
	steam_event_key VARCHAR(60),
	source VARCHAR(20) DEFAULT 'steam' NOT NULL,
	product_unknown BOOLEAN DEFAULT '0' NOT NULL,
	snapshot_at DATETIME,
	PRIMARY KEY (id)
)`,
	`CREATE UNIQUE INDEX ux_gph_snapshot ON game_price_history (
appid, region_code, snapshot_at, COALESCE(sub_id, -1),
COALESCE(price, 0), COALESCE(is_gold, 0))`,
	`CREATE INDEX ix_gph_snapshot_at ON game_price_history(snapshot_at)`,
	`CREATE TABLE app_settings (
	key VARCHAR(64) NOT NULL,
	value_json JSON,
	updated_at DATETIME,
	PRIMARY KEY (key)
)`,
	`CREATE TABLE fx_rates (
	currency_code VARCHAR(10) NOT NULL,
	rate_to_cny REAL NOT NULL,
	fetched_at DATETIME,
	PRIMARY KEY (currency_code)
)`,
}

func newTestWriter(t *testing.T) (*Writer, *sql.DB) {
	t.Helper()
	dbPath := filepath.Join(t.TempDir(), "test.db")
	db, err := store.Open(dbPath)
	if err != nil {
		t.Fatalf("打开测试库失败: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	for _, ddl := range testDDL {
		if _, err := db.Exec(ddl); err != nil {
			t.Fatalf("建表失败: %v", err)
		}
	}
	gate := store.NewWriteGate(db, dbPath)
	return NewWriter(db, gate), db
}

func testNow() time.Time {
	return time.Date(2026, 10, 31, 12, 30, 45, 123456000, BeijingLoc)
}

func strPtr(s string) *string { return &s }
func i64Ptr(i int64) *int64   { return &i }
func boolPtr(b bool) *bool    { return &b }
func gameData(appid int64, name string) GameData {
	return gameDataAt(appid, name, testNow())
}

// gameDataAt 元数据夹具：UpdatedAt/CreatedAt 定格到指定挂钟（game_data
// 携带的 updated_at 优先于写层时钟，与 Python 同语义）。
func gameDataAt(appid int64, name string, at time.Time) GameData {
	n := at
	return GameData{
		AppID: appid, Name: strPtr(name), NameEn: strPtr(name),
		Type: strPtr("GAME"), HeaderImage: strPtr("h.jpg"),
		FamilySharing: boolPtr(false), TradingCards: boolPtr(false),
		IsAdult: boolPtr(false), IsVisualNovel: boolPtr(false),
		ReleaseDate:  strPtr("2024-01-01"),
		Developers:   []any{"Dev One", "Dev Two"},
		Publishers:   []any{"Pub One"},
		PositiveRate: i64Ptr(8000), PositiveReviews: i64Ptr(80), ReviewCount: i64Ptr(100),
		CreatedAt: &n, UpdatedAt: &n,
	}
}

func priceRow(appid int64, region string, price int64) PriceRow {
	return PriceRow{
		AppID: appid, RegionCode: strPtr(region), Currency: strPtr("USD"),
		Price: i64Ptr(price), OriginalPrice: i64Ptr(price), DiscountPercent: i64Ptr(0),
		SubID: i64Ptr(0), IsGold: boolPtr(false), IsBundle: boolPtr(false),
		PriceStatus: strPtr("ok"),
	}
}

// dumpRows 通用逐行 dump：DATETIME 列 CAST 出原文，规避驱动扫描伪影。
func dumpRows(t *testing.T, db *sql.DB, query string) []map[string]any {
	t.Helper()
	rows, err := db.Query(query)
	if err != nil {
		t.Fatalf("dump 查询失败: %v", err)
	}
	defer rows.Close()
	cols, err := rows.Columns()
	if err != nil {
		t.Fatalf("取列名失败: %v", err)
	}
	var out []map[string]any
	for rows.Next() {
		vals := make([]any, len(cols))
		ptrs := make([]any, len(cols))
		for i := range vals {
			ptrs[i] = &vals[i]
		}
		if err := rows.Scan(ptrs...); err != nil {
			t.Fatalf("dump 扫描失败: %v", err)
		}
		m := map[string]any{}
		for i, c := range cols {
			m[c] = vals[i]
		}
		out = append(out, m)
	}
	if err := rows.Err(); err != nil {
		t.Fatalf("dump 遍历失败: %v", err)
	}
	return out
}

func dumpCurrent(t *testing.T, db *sql.DB) []map[string]any {
	return dumpRows(t, db, `SELECT appid, region_code, currency, price, original_price,
discount_percent, sub_id, price_status, fail_count, cny_fen, discount_end_ts,
attempt_outcome, steam_answer, CAST(last_success_at AS TEXT) AS last_success_at,
CAST(updated_at AS TEXT) AS updated_at FROM game_current_prices ORDER BY appid, region_code`)
}

func dumpHistory(t *testing.T, db *sql.DB) []map[string]any {
	return dumpRows(t, db, `SELECT id, appid, region_code, currency, price, original_price,
discount_percent, sub_id, is_gold, version_suffix, is_bundle, price_status, cny_fen,
discount_end_ts, discount_desc, bundle_id, bundle_discount_pct, steam_event_key,
source, CAST(snapshot_at AS TEXT) AS snapshot_at
FROM game_price_history ORDER BY id`)
}

func dumpGameCol(t *testing.T, db *sql.DB, appid int64, col string) any {
	t.Helper()
	var v any
	if err := db.QueryRow("SELECT "+col+" FROM games WHERE appid=?", appid).Scan(&v); err != nil {
		t.Fatalf("读 games %s 失败: %v", col, err)
	}
	return v
}

func runBatch(t *testing.T, w *Writer, entries []BatchEntry) []bool {
	t.Helper()
	results, err := w.UpsertTaskBatch(context.Background(), entries)
	if err != nil {
		t.Fatalf("批量写失败: %v", err)
	}
	return results
}

// TestReplayKeepsRowIdentity 同观测重放：唯一键冲突走 DO UPDATE，非键详情
// 更新、原行 ID 与旧促销描述（COALESCE 兜底）保留。
func TestReplayKeepsRowIdentity(t *testing.T) {
	w, db := newTestWriter(t)
	w.Now = testNow
	w.FxRates = map[string]float64{"USD": 7.25}
	w.EventKey = func(time.Time) *string { return strPtr("seasonal_fixture") }

	p := priceRow(1001, "CN", 19900)
	p.DiscountDesc = strPtr("首个折扣")
	results := runBatch(t, w, []BatchEntry{{Game: gameData(1001, "游戏甲"), Prices: []PriceRow{p}}})
	if !results[0] {
		t.Fatal("首写应成功")
	}
	first := dumpHistory(t, db)
	if len(first) != 1 {
		t.Fatalf("应落 1 行历史，实际 %d", len(first))
	}
	origID := first[0]["id"]

	// 重放：价格/原价/版本/gold 不变（差量门禁放行），折扣比例变化 →
	// 唯一键仍冲突（同观测时刻同价）→ 更新详情
	p2 := p
	p2.DiscountPercent = i64Ptr(10)
	p2.DiscountDesc = nil
	p2.BundleID = i64Ptr(77)
	w.EventKey = func(time.Time) *string { return strPtr("revised_fixture") }
	runBatch(t, w, []BatchEntry{{Game: gameData(1001, "游戏甲"), Prices: []PriceRow{p2}}})

	second := dumpHistory(t, db)
	if len(second) != 1 {
		t.Fatalf("重放不得新增历史行，实际 %d", len(second))
	}
	if second[0]["id"] != origID {
		t.Fatalf("重放必须保留原行 ID：旧 %v 新 %v", origID, second[0]["id"])
	}
	if second[0]["discount_percent"] != int64(10) {
		t.Fatalf("重放应更新折扣比例，实际 %v", second[0]["discount_percent"])
	}
	if second[0]["discount_desc"] != "首个折扣" {
		t.Fatalf("促销描述 nil 应保留旧值，实际 %v", second[0]["discount_desc"])
	}
	if second[0]["bundle_id"] != int64(77) {
		t.Fatalf("重放应更新 bundle_id，实际 %v", second[0]["bundle_id"])
	}
	if second[0]["steam_event_key"] != "seasonal_fixture" {
		t.Fatalf("重放改写已记录的活动标签：%v", second[0]["steam_event_key"])
	}
}

// TestFailedAttemptKeepsLastGoodPrice 失败章保底：失败观察只推进尝试，
// 价格三件套与最近成功时刻保留旧行值。
func TestFailedAttemptKeepsLastGoodPrice(t *testing.T) {
	w, db := newTestWriter(t)
	w.Now = testNow
	w.FxRates = map[string]float64{"USD": 7.25}

	runBatch(t, w, []BatchEntry{{Game: gameData(1002, "游戏乙"), Prices: []PriceRow{priceRow(1002, "CN", 19900)}}})
	before := dumpCurrent(t, db)
	if len(before) != 1 {
		t.Fatalf("应落 1 行现价，实际 %d", len(before))
	}

	// 第二轮：ok 但无价格（坏数据）→ 门禁降级 → 失败章
	w.Now = func() time.Time { return testNow().Add(time.Hour) }
	bad := priceRow(1002, "CN", 0)
	bad.Price = nil
	runBatch(t, w, []BatchEntry{{Game: gameDataAt(1002, "游戏乙", testNow().Add(time.Hour)), Prices: []PriceRow{bad}}})

	after := dumpCurrent(t, db)[0]
	if after["price"] != int64(19900) {
		t.Fatalf("失败观察必须保留旧价，实际 %v", after["price"])
	}
	if after["last_success_at"] != "2026-10-31 12:30:45.123456" {
		t.Fatalf("最近成功时刻不得回退，实际 %v", after["last_success_at"])
	}
	if after["updated_at"] != "2026-10-31 13:30:45.123456" {
		t.Fatalf("尝试时刻应推进，实际 %v", after["updated_at"])
	}
	if after["attempt_outcome"] != "failed" {
		t.Fatalf("应盖失败章，实际 %v", after["attempt_outcome"])
	}
	if after["steam_answer"] != nil {
		t.Fatalf("失败章应清答复，实际 %v", after["steam_answer"])
	}
	if after["fail_count"] != int64(1) {
		t.Fatalf("真实缺价应递增欠账，实际 %v", after["fail_count"])
	}
	if after["price_status"] != "missing" {
		t.Fatalf("一次缺价应为 missing，实际 %v", after["price_status"])
	}
}

// TestMissingFiveTimesTurnsBlocked 连续 5 次真实缺价穷尽补抓预算转终态。
func TestMissingFiveTimesTurnsBlocked(t *testing.T) {
	w, db := newTestWriter(t)
	w.Now = testNow
	w.FxRates = map[string]float64{"CNY": 1.0}

	bad := priceRow(1003, "CN", 0)
	bad.Price = nil
	entries := []BatchEntry{{Game: gameData(1003, "游戏丙"), Prices: []PriceRow{bad}}}
	for i := 1; i <= 5; i++ {
		runBatch(t, w, entries)
		row := dumpCurrent(t, db)[0]
		if got := row["fail_count"]; got != int64(i) {
			t.Fatalf("第 %d 轮 fail_count 应为 %d，实际 %v", i, i, got)
		}
		wantStatus := "missing"
		if i >= missingMaxRetries {
			wantStatus = "blocked"
		}
		if row["price_status"] != wantStatus {
			t.Fatalf("第 %d 轮状态应为 %s，实际 %v", i, wantStatus, row["price_status"])
		}
	}
}

// TestFreeKindGrouping 免费态分组：带免费态字段的组随行落标记；纯
// locked/missing 响应（无 ok 行）不进语句不洗标记；CN 付费行显式清标记。
func TestFreeKindGrouping(t *testing.T) {
	w, db := newTestWriter(t)
	w.Now = testNow
	w.FxRates = map[string]float64{"CNY": 1.0}

	promo := priceRow(2001, "CN", 0)
	promo.OriginalPrice = i64Ptr(100000)
	promo.PromoEndTS = i64Ptr(1800000000)
	f2p := priceRow(2002, "CN", 0)
	f2p.OriginalPrice = i64Ptr(0)
	runBatch(t, w, []BatchEntry{
		{Game: gameData(2001, "赠送游戏"), Prices: []PriceRow{promo}},
		{Game: gameData(2002, "免费游戏"), Prices: []PriceRow{f2p}},
		{Game: gameData(2003, "付费游戏"), Prices: []PriceRow{priceRow(2003, "CN", 59900)}},
	})
	if got := dumpGameCol(t, db, 2001, "free_kind"); got != "promo" {
		t.Fatalf("限时赠送应标 promo，实际 %v", got)
	}
	if got := dumpGameCol(t, db, 2001, "promo_end_at"); got != int64(1800000000) {
		t.Fatalf("赠送结束时刻应随行落库，实际 %v", got)
	}
	if got := dumpGameCol(t, db, 2002, "free_kind"); got != "f2p" {
		t.Fatalf("永久免费应标 f2p，实际 %v", got)
	}
	if got := dumpGameCol(t, db, 2003, "free_kind"); got != nil {
		t.Fatalf("付费游戏不应有免费标记，实际 %v", got)
	}
	// 限时赠送 price=0+original>0 是真实价格事件照写历史；付费行照写；
	// 永久免费 price=0+original=0 不进历史
	hist := dumpHistory(t, db)
	if len(hist) != 2 {
		t.Fatalf("赠送与付费各落一行历史，实际 %+v", hist)
	}
	seen := map[int64]bool{}
	for _, h := range hist {
		seen[h["appid"].(int64)] = true
	}
	if !seen[2001] || !seen[2003] || seen[2002] {
		t.Fatalf("历史行集应为 {2001, 2003}，实际 %+v", hist)
	}

	// 第二批：locked-only（无 ok 行）不洗 promo；付费 CN 行显式清 promo
	locked := priceRow(2001, "CN", 0)
	locked.Price = nil
	locked.PriceStatus = strPtr("locked")
	paidAgain := priceRow(2001+3, "CN", 59900) // appid 2004 造 promo 后付费
	// 先给 2004 打 promo
	runBatch(t, w, []BatchEntry{{
		Game:   gameData(2004, "再赠送"),
		Prices: []PriceRow{func() PriceRow { p := priceRow(2004, "CN", 0); p.OriginalPrice = i64Ptr(500); return p }()},
	}})
	if got := dumpGameCol(t, db, 2004, "free_kind"); got != "promo" {
		t.Fatalf("预置 promo 失败，实际 %v", got)
	}
	runBatch(t, w, []BatchEntry{
		{Game: gameData(2001, "赠送游戏"), Prices: []PriceRow{locked}},
		{Game: gameData(2004, "再赠送"), Prices: []PriceRow{paidAgain}},
	})
	if got := dumpGameCol(t, db, 2001, "free_kind"); got != "promo" {
		t.Fatalf("纯 locked 响应不得洗掉免费态，实际 %v", got)
	}
	if got := dumpGameCol(t, db, 2004, "free_kind"); got != nil {
		t.Fatalf("CN 付费行应显式清标记，实际 %v", got)
	}
}

// TestChunkRollbackNoPartialBatch 段内任意语句失败整段回退，不落半批。
func TestChunkRollbackNoPartialBatch(t *testing.T) {
	w, db := newTestWriter(t)
	w.Now = testNow
	w.FxRates = map[string]float64{"USD": 7.25}

	broken := gameData(3002, "坏行")
	broken.Name = nil // name 非空约束 → INSERT 失败
	results, err := w.UpsertTaskBatch(context.Background(), []BatchEntry{
		{Game: gameData(3001, "好行"), Prices: []PriceRow{priceRow(3001, "CN", 19900)}},
		{Game: broken, Prices: []PriceRow{priceRow(3002, "CN", 9900)}},
	})
	if err != nil {
		t.Fatalf("段体失败应吞掉转结果标记，不应上抛: %v", err)
	}
	if results[0] || results[1] {
		t.Fatalf("整段失败应全部标记未写入，实际 %v", results)
	}
	for table, want := range map[string]int{"games": 0, "game_current_prices": 0, "game_price_history": 0} {
		var n int
		if err := db.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&n); err != nil {
			t.Fatalf("计数失败: %v", err)
		}
		if n != want {
			t.Fatalf("%s 应为空（不落半批），实际 %d 行", table, n)
		}
	}
}

// TestVersionSuffixBackfill 丢名行沿用同 sub 的历史名后再做差量比较。
func TestVersionSuffixBackfill(t *testing.T) {
	w, db := newTestWriter(t)
	w.Now = testNow
	w.FxRates = map[string]float64{"USD": 7.25}

	first := priceRow(4001, "CN", 19900)
	first.VersionSuffix = strPtr("Deluxe Edition")
	runBatch(t, w, []BatchEntry{{Game: gameData(4001, "版本游戏"), Prices: []PriceRow{first}}})

	// 第二轮丢名 + 涨价：回填历史名后与 prev 比较（价格变了 → 新快照）
	w.Now = func() time.Time { return testNow().Add(time.Hour) }
	second := priceRow(4001, "CN", 25900)
	second.VersionSuffix = nil
	runBatch(t, w, []BatchEntry{{Game: gameDataAt(4001, "版本游戏", testNow().Add(time.Hour)), Prices: []PriceRow{second}}})

	hist := dumpHistory(t, db)
	if len(hist) != 2 {
		t.Fatalf("应落 2 行历史，实际 %d", len(hist))
	}
	if hist[1]["version_suffix"] != "Deluxe Edition" {
		t.Fatalf("丢名行应回填同 sub 历史名，实际 %v", hist[1]["version_suffix"])
	}
}

// TestOkRegionsClearFailCount 补抓成功结转：missing → 成功 → 回 ok 清欠账。
func TestOkRegionsClearFailCount(t *testing.T) {
	w, db := newTestWriter(t)
	w.Now = testNow
	w.FxRates = map[string]float64{"CNY": 1.0}

	bad := priceRow(5001, "CN", 0)
	bad.Price = nil
	entries := []BatchEntry{{Game: gameData(5001, "游戏丁"), Prices: []PriceRow{bad}}}
	runBatch(t, w, entries)
	runBatch(t, w, entries)
	if got := dumpCurrent(t, db)[0]["fail_count"]; got != int64(2) {
		t.Fatalf("预置欠账失败，实际 %v", got)
	}

	w.Now = func() time.Time { return testNow().Add(2 * time.Hour) }
	runBatch(t, w, []BatchEntry{{Game: gameDataAt(5001, "游戏丁", testNow().Add(2*time.Hour)), Prices: []PriceRow{priceRow(5001, "CN", 19900)}}})
	row := dumpCurrent(t, db)[0]
	if row["fail_count"] != int64(0) {
		t.Fatalf("补抓成功应清欠账，实际 %v", row["fail_count"])
	}
	if row["price_status"] != "ok" || row["attempt_outcome"] != "success" {
		t.Fatalf("应回到成功态，实际 %v / %v", row["price_status"], row["attempt_outcome"])
	}
	if row["last_success_at"] != "2026-10-31 14:30:45.123456" {
		t.Fatalf("成功观察应推进最近成功时刻，实际 %v", row["last_success_at"])
	}
}

// TestMarkRegionStatusMissingBatch 传输类失败只盖失败章：不递增欠账、
// 不改价格状态，保留 last good price。
func TestMarkRegionStatusMissingBatch(t *testing.T) {
	w, db := newTestWriter(t)
	w.Now = testNow
	w.FxRates = map[string]float64{"USD": 7.25}

	runBatch(t, w, []BatchEntry{{Game: gameData(6001, "游戏戊"), Prices: []PriceRow{priceRow(6001, "CN", 19900)}}})

	w.Now = func() time.Time { return testNow().Add(time.Hour) }
	n, err := w.MarkRegionStatusMissingBatch(context.Background(), []RegionPair{
		{AppID: 6001, RegionCode: "cn"},
		{AppID: 6001, RegionCode: "CN"}, // 同批重复：去重防双章
		{AppID: 6002, RegionCode: "US"},
	})
	if err != nil {
		t.Fatalf("批量记缺失失败: %v", err)
	}
	if n != 2 {
		t.Fatalf("去重后应写 2 行，实际 %d", n)
	}
	rows := dumpCurrent(t, db)
	byKey := map[string]map[string]any{}
	for _, r := range rows {
		byKey[r["region_code"].(string)] = r
	}
	cn := byKey["CN"]
	if cn["price"] != int64(19900) {
		t.Fatalf("失败章必须保留旧价，实际 %v", cn["price"])
	}
	if cn["fail_count"] != int64(0) {
		t.Fatalf("传输类失败不消耗补抓预算，实际 %v", cn["fail_count"])
	}
	if cn["attempt_outcome"] != "failed" || cn["steam_answer"] != nil {
		t.Fatalf("应盖失败章清答复，实际 %v / %v", cn["attempt_outcome"], cn["steam_answer"])
	}
	if cn["updated_at"] != "2026-10-31 13:30:45.123456" {
		t.Fatalf("失败章应刷新时刻，实际 %v", cn["updated_at"])
	}
	us := byKey["US"]
	if us["price"] != nil || us["fail_count"] != int64(1) || us["price_status"] != "missing" {
		t.Fatalf("新账行形状不符，实际 %+v", us)
	}
}

// TestPyDumpsAligned Python json.dumps 缺省参数逐字节对齐。
func TestPyDumpsAligned(t *testing.T) {
	cases := []struct {
		in   any
		want string
	}{
		{nil, "null"},
		{[]any{}, "[]"},
		{[]any{"Valve", "中文社"}, `["Valve", "\u4e2d\u6587\u793e"]`},
		{[]any{any(nil), true, int64(3)}, "[null, true, 3]"},
		{[]any{"a\"b\\c\n"}, `["a\"b\\c\n"]`},
		{"\U0001F600", `"\ud83d\ude00"`},
	}
	for _, c := range cases {
		if got := PyDumps(c.in); got != c.want {
			t.Fatalf("PyDumps(%v) = %s, want %s", c.in, got, c.want)
		}
	}
}
