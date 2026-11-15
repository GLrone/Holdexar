package derived

// B11 MaintainDerived 日切换全库重算：版本相同零重算、跨日两段重算 +
// 成员捆绑联动、系列覆盖变化触发、覆盖文件联动（B7 装填链）。

import (
	"context"
	"database/sql"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
)

func newMaintainDB(t *testing.T) (*sql.DB, *store.WriteGate) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "maintain.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	for _, ddl := range testTables {
		if _, err := db.Exec(ddl); err != nil {
			t.Fatal(err)
		}
	}
	// 成员捆绑联动与发现桩关联查询的两张捆绑包表（生产 DDL 同形）
	for _, ddl := range []string{
		`CREATE TABLE bundle_region_prices (
	id INTEGER NOT NULL, bundle_id INTEGER NOT NULL, region_code VARCHAR(10) NOT NULL,
	currency VARCHAR(10), price BIGINT, original_price BIGINT,
	discount_percent INTEGER NOT NULL, bundle_base_discount INTEGER NOT NULL,
	price_status VARCHAR(20) NOT NULL, cny_fen BIGINT, discount_end_ts INTEGER,
	app_ids JSON, crawled_at DATETIME, PRIMARY KEY (id))`,
		`CREATE TABLE bundles (
	id INTEGER NOT NULL, bundle_id INTEGER NOT NULL, name VARCHAR(512) NOT NULL,
	must_purchase_as_set INTEGER NOT NULL, item_kind INTEGER NOT NULL,
	header_image VARCHAR(1024), is_lowest BOOLEAN NOT NULL, min_cny_fen BIGINT,
	diff_fen INTEGER DEFAULT '0' NOT NULL, smart_score FLOAT, url VARCHAR(1024),
	app_ids JSON, view_count INTEGER NOT NULL, updated_at DATETIME,
	PRIMARY KEY (id), UNIQUE (bundle_id))`,
	} {
		if _, err := db.Exec(ddl); err != nil {
			t.Fatal(err)
		}
	}
	return db, store.NewWriteGate(db, path)
}

func maintainGame(t *testing.T, db *sql.DB, appid int64, name string, rc int64) {
	t.Helper()
	mustExec(t, db, `INSERT INTO games (appid, name, type, hl_flag, pp_flag, diff_fen,
		is_adult, is_visual_novel, positive_rate, positive_reviews, review_count)
		VALUES (?, ?, 'GAME', 0, 0, 0, 0, 0, 8000, 0, ?)`, appid, name, rc)
}

func maintainCN(t *testing.T, db *sql.DB, appid int64, price int64) {
	t.Helper()
	mustExec(t, db, `INSERT INTO game_current_prices (appid, region_code, currency, price,
		original_price, discount_percent, sub_id, price_status, fail_count, cny_fen,
		attempt_outcome, updated_at) VALUES (?, 'CN', 'CNY', ?, ?, 0, 0, 'ok', 0, ?, 'success', ?)`,
		appid, price, price, price, cmpNow)
}

func maintainCol(t *testing.T, db *sql.DB, appid int64, col string) any {
	t.Helper()
	var v any
	if err := db.QueryRow("SELECT "+col+" FROM games WHERE appid=?", appid).Scan(&v); err != nil {
		t.Fatal(err)
	}
	return v
}

func savedVersionOf(t *testing.T, db *sql.DB) string {
	t.Helper()
	var raw sql.NullString
	if err := db.QueryRow(`SELECT value_json FROM app_settings WHERE key='games.derived_inputs'`).Scan(&raw); err != nil && !errors.Is(err, sql.ErrNoRows) {
		t.Fatal(err)
	}
	if !raw.Valid {
		return ""
	}
	return raw.String
}

// ①版本相同零重算：首跑建版本，同版本重跑不改任何派生列。
func TestMaintainDerivedSameVersionSkipsRecompute(t *testing.T) {
	db, gate := newMaintainDB(t)
	ctx := context.Background()
	now := cmpNow
	maintainGame(t, db, 1, "甲", 0) // 评测零 → 首跑 score 段即重算
	maintainCN(t, db, 1, 1999)
	insertRegion(t, db, 1, "US", 2000, 100) // min_cny_fen = 非 CN 最低羊毛价
	if err := MaintainDerived(ctx, db, gate, now, nil, nil); err != nil {
		t.Fatal(err)
	}
	if got := maintainCol(t, db, 1, "min_cny_fen"); got != int64(100) {
		t.Fatalf("首跑应重算派生列: %v", got)
	}
	saved := savedVersionOf(t, db)
	if saved == "" {
		t.Fatal("首跑应保存版本")
	}
	// 人工破坏派生列 + 系列归属：版本一致的重跑必须原样保留
	mustExec(t, db, `UPDATE games SET min_cny_fen=999, series_id='junk' WHERE appid=1`)
	if err := MaintainDerived(ctx, db, gate, now, nil, nil); err != nil {
		t.Fatal(err)
	}
	if got := maintainCol(t, db, 1, "min_cny_fen"); got != int64(999) {
		t.Fatalf("版本相同应零重算: %v", got)
	}
	if got := maintainCol(t, db, 1, "series_id"); got != "junk" {
		t.Fatalf("版本相同不应重算系列: %v", got)
	}
	if savedVersionOf(t, db) != saved {
		t.Fatal("版本文档不应变化")
	}
}

// ②跨日切换：hl_flag=1 行带历史重算、评测零行重算（不含历史）+ 成员捆绑联动。
func TestMaintainDerivedDaySwitchRecomputes(t *testing.T) {
	db, gate := newMaintainDB(t)
	ctx := context.Background()
	yesterday := cmpNow.AddDate(0, 0, -1)
	maintainGame(t, db, 1, "甲", 100) // 评测非零 → hl 段重算
	maintainCN(t, db, 1, 1999)
	insertRegion(t, db, 1, "US", 2000, 100)
	maintainGame(t, db, 2, "乙", 0) // 评测零 → score 段重算 + 捆绑联动
	maintainCN(t, db, 2, 5000)
	mustExec(t, db, `INSERT INTO bundle_region_prices (id, bundle_id, region_code, price, discount_percent, bundle_base_discount, price_status, app_ids)
		VALUES (1, 9, 'US', 100, 0, 0, 'ok', '[2]')`)
	var hooked [][]int64
	RefreshBundleSortCache = func(ctx context.Context, conn *sql.Conn, bundleIDs []int64) error {
		hooked = append(hooked, bundleIDs)
		return nil
	}
	t.Cleanup(func() { RefreshBundleSortCache = nil })

	if err := MaintainDerived(ctx, db, gate, yesterday, nil, nil); err != nil {
		t.Fatal(err)
	}
	// 昨日收尾状态：史低标记在场（首跑已按当日数据归位，这里回置模拟昨日
	// 的新低期），派生列随后被人工破坏
	mustExec(t, db, `UPDATE games SET hl_flag=1, min_cny_fen=0, smart_score=NULL WHERE appid=1`)
	mustExec(t, db, `UPDATE games SET min_cny_fen=0, smart_score=NULL WHERE appid=2`)
	if err := MaintainDerived(ctx, db, gate, cmpNow, nil, nil); err != nil {
		t.Fatal(err)
	}
	if got := maintainCol(t, db, 1, "min_cny_fen"); got != int64(100) {
		t.Fatalf("hl 行应带历史重算: %v", got)
	}
	if got := maintainCol(t, db, 2, "smart_score"); got == nil {
		t.Fatal("评测零行应重算评分")
	}
	// 首跑（昨日）与日切换（今日）各走一次 score 段，均联动捆绑刷新
	//（钩子收到的是受影响 appid 所在捆绑包的 bundle_id=9）
	if len(hooked) != 2 || len(hooked[1]) != 1 || hooked[1][0] != 9 {
		t.Fatalf("成员捆绑联动应随 score 段触发: %v", hooked)
	}
	if day := savedVersionOf(t, db); !contains(day, cmpNow.Format("2006-01-02")) {
		t.Fatalf("版本应保存新日: %s", day)
	}
}

// ③系列覆盖变化触发全量重算（同日不触发两段重算）。
func TestMaintainDerivedSeriesChangeRefreshes(t *testing.T) {
	db, gate := newMaintainDB(t)
	ctx := context.Background()
	mustExec(t, db, `INSERT INTO games (appid, name, name_en, type, hl_flag, pp_flag, diff_fen,
		is_adult, is_visual_novel, positive_reviews, review_count)
		VALUES (1, '阿尔法一号', 'Alpha One', 'GAME', 0, 0, 0, 0, 0, 0, 0),
		       (2, '贝塔二号', 'Beta Two', 'GAME', 0, 0, 0, 0, 0, 0, 0)`)
	overrides := map[string][]int64{"命运系列": {1, 2}}
	if err := MaintainDerived(ctx, db, gate, cmpNow, overrides, nil); err != nil {
		t.Fatal(err)
	}
	for _, id := range []int64{1, 2} {
		if got := maintainCol(t, db, id, "series_id"); got != "命运系列" {
			t.Fatalf("覆盖表应落位: %d → %v", id, got)
		}
	}
	// 覆盖改名：series 变化再次触发；day 不变不触发两段重算
	mustExec(t, db, `UPDATE games SET min_cny_fen=999 WHERE appid IN (1,2)`)
	overrides2 := map[string][]int64{"新纪元": {1, 2}}
	if err := MaintainDerived(ctx, db, gate, cmpNow, overrides2, nil); err != nil {
		t.Fatal(err)
	}
	for _, id := range []int64{1, 2} {
		if got := maintainCol(t, db, id, "series_id"); got != "新纪元" {
			t.Fatalf("系列变化应触发重算: %d → %v", id, got)
		}
		if got := maintainCol(t, db, id, "min_cny_fen"); got != int64(999) {
			t.Fatalf("同日不得触发 hl 段重算: %d → %v", id, got)
		}
	}
}

// ④与 B7 联动：覆盖文件经 LoadSeriesOverrides 装填进 MaintainDerived；
// 文件变更 → 版本变化 → 下一轮重算系列。
func TestMaintainDerivedFollowsOverrideFile(t *testing.T) {
	db, gate := newMaintainDB(t)
	ctx := context.Background()
	dataDir := t.TempDir()
	file := filepath.Join(dataDir, "series_overrides.json")
	write := func(body string) {
		t.Helper()
		if err := os.WriteFile(file, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	mustExec(t, db, `INSERT INTO games (appid, name, name_en, type, hl_flag, pp_flag, diff_fen,
		is_adult, is_visual_novel, positive_reviews, review_count)
		VALUES (1, '阿尔法一号', 'Alpha One', 'GAME', 0, 0, 0, 0, 0, 0, 0)`)
	write(`{"首章": [1]}`)
	overrides, order := LoadSeriesOverrides(dataDir)
	if err := MaintainDerived(ctx, db, gate, cmpNow, overrides, order); err != nil {
		t.Fatal(err)
	}
	if got := maintainCol(t, db, 1, "series_id"); got != "首章" {
		t.Fatalf("覆盖文件应落位: %v", got)
	}
	write(`{"次章": [1]}`)
	overrides, order = LoadSeriesOverrides(dataDir)
	if err := MaintainDerived(ctx, db, gate, cmpNow, overrides, order); err != nil {
		t.Fatal(err)
	}
	if got := maintainCol(t, db, 1, "series_id"); got != "次章" {
		t.Fatalf("覆盖文件变更应联动重算: %v", got)
	}
}

func contains(haystack, needle string) bool {
	return len(needle) > 0 && len(haystack) >= len(needle) &&
		func() bool {
			for i := 0; i+len(needle) <= len(haystack); i++ {
				if haystack[i:i+len(needle)] == needle {
					return true
				}
			}
			return false
		}()
}

var _ = time.Now
