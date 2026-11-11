package crawl

import (
	"context"
	"database/sql"
	"reflect"
	"testing"
	"time"
)

func TestDataMetadataSettingsAndCurrencyUseExistingRows(t *testing.T) {
	db, gate := runtimeFixture(t)
	r := New(db, gate, nil, Options{})
	ctx := context.Background()
	planExec(t, db, `INSERT INTO games(appid,name,name_en,type,header_image,store_url,chinese_support,family_sharing,trading_cards,is_epic,is_hb,hl_flag,pp_flag,diff_fen,is_adult,is_visual_novel,removed_strikes,developers,publishers,positive_rate,positive_reviews,review_count,view_count,release_date,created_at,updated_at) VALUES (220,'半条命','Half-Life 2','GAME','header','store','full',1,0,0,0,0,0,0,0,1,0,'["Valve","维尔福"]','["Valve"]',9700,97,100,0,'2004-11-16','2026-11-06 01:02:03.123456','2026-11-06 02:03:04.654321')`)
	ids := make([]int64, 601)
	for i := range ids {
		ids[i] = int64(i + 1)
	}
	rows, err := r.loadMetadata(ctx, ids)
	if err != nil {
		t.Fatal(err)
	}
	g := rows[220]
	if len(rows) != 1 || g.Name == nil || *g.Name != "半条命" || *g.NameEn != "Half-Life 2" || *g.Type != "GAME" || *g.HeaderImage != "header" || *g.StoreURL != "store" || *g.ChineseSupport != "full" || !*g.FamilySharing || *g.TradingCards || *g.IsAdult || !*g.IsVisualNovel || *g.ReleaseDate != "2004-11-16" || *g.PositiveRate != 9700 || *g.PositiveReviews != 97 || *g.ReviewCount != 100 || g.CreatedAt.Nanosecond() != 123456000 || g.UpdatedAt.Nanosecond() != 654321000 || !reflect.DeepEqual(g.Developers, []any{"Valve", "维尔福"}) {
		t.Fatalf("%+v", g)
	}
	if v, err := r.setting(ctx, "test.absent", "fallback"); err != nil || v != "fallback" {
		t.Fatal(v, err)
	}
	planExec(t, db, `INSERT INTO app_settings(key,value_json) VALUES ('test.setting','"direct_only"')`)
	if v, err := r.setting(ctx, "test.setting", nil); err != nil || v != "direct_only" {
		t.Fatal(v, err)
	}
	planExec(t, db, `UPDATE app_settings SET value_json='null' WHERE key='test.setting'`)
	if v, err := r.setting(ctx, "test.setting", "fallback"); err != nil || v != nil {
		t.Fatal(v, err)
	}
	planExec(t, db, `UPDATE app_settings SET value_json='bad json' WHERE key='test.setting'`)
	if _, err := r.setting(ctx, "test.setting", nil); err == nil {
		t.Fatal("malformed stored setting accepted")
	}
	currencies := map[string]string{"cn": "CNY"}
	if err := r.loadTaskCurrencies(ctx, []TargetBatch{{Region: "us"}, {Region: "jp"}, {Region: "cn"}}, currencies); err != nil || currencies["us"] != "USD" || currencies["jp"] != "JPY" {
		t.Fatal(currencies, err)
	}
	if err := r.loadTaskCurrencies(ctx, []TargetBatch{{Region: "zz"}}, currencies); err == nil {
		t.Fatal("unknown currency fabricated")
	}
}

func dataObservation(t *testing.T, db *sql.DB, id int64, region, status string, outcome, answer any, updated, success any) {
	t.Helper()
	planExec(t, db, `INSERT INTO game_current_prices(appid,region_code,price_status,discount_percent,fail_count,attempt_outcome,steam_answer,updated_at,last_success_at) VALUES (?,?,?,0,0,?,?,?,?)`, id, region, status, outcome, answer, updated, success)
}

type frozenDataStats struct{ total, done, expected, ok, locked, failed, missing, blocked, unobserved, stale int }

func readDataStats(t *testing.T, db *sql.DB, id int64) frozenDataStats {
	t.Helper()
	var s frozenDataStats
	err := db.QueryRow(`SELECT targets_total,targets_done,units_expected,units_ok,units_locked,units_failed,units_missing,units_blocked,units_unobserved,stale_count FROM price_cycles WHERE id=?`, id).Scan(&s.total, &s.done, &s.expected, &s.ok, &s.locked, &s.failed, &s.missing, &s.blocked, &s.unobserved, &s.stale)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func TestDataFreezeCountsCanonicalBucketsAndExactWindow(t *testing.T) {
	db, gate := runtimeFixture(t)
	r := New(db, gate, nil, Options{})
	start := time.Date(2026, 11, 6, 12, 0, 0, 123456000, beijing)
	end := start.Add(time.Hour)
	planExec(t, db, `INSERT INTO price_cycles(id,kind,status,scope,expected_json) VALUES (1,'manual','finalizing','pool','{"appids":[1,2,3,4,5],"regions":["cn","US","cn"]}')`)
	dataObservation(t, db, 1, "CN", "ok", "success", "ok", timestamp(start), timestamp(end))
	dataObservation(t, db, 1, "US", "ok", "success", "no_options", timestamp(end), timestamp(end))
	dataObservation(t, db, 2, "CN", "ok", "failed", "ok", timestamp(start.Add(time.Minute)), timestamp(end.Add(-13*time.Hour)))
	dataObservation(t, db, 2, "US", "blocked", "failed", nil, timestamp(end), nil)
	dataObservation(t, db, 3, "CN", "ok", "success", "ok", timestamp(start.Add(-time.Microsecond)), timestamp(end.Add(-12*time.Hour)))
	dataObservation(t, db, 3, "US", "ok", "success", "ok", timestamp(end.Add(time.Microsecond)), nil)
	dataObservation(t, db, 4, "CN", " OK ", nil, nil, timestamp(start), nil)
	dataObservation(t, db, 4, "US", "missing", "success", "locked", timestamp(start), nil)
	dataObservation(t, db, 1, "JP", "ok", "success", "ok", timestamp(start), timestamp(end.Add(-100*time.Hour)))
	dataObservation(t, db, 99, "CN", "ok", "success", "ok", timestamp(start), timestamp(end.Add(-100*time.Hour)))
	if err := r.freezeStats(context.Background(), 1, []int64{99}, []Region{{"jp", "JPY"}}, start, end); err != nil {
		t.Fatal(err)
	}
	got := readDataStats(t, db, 1)
	want := frozenDataStats{total: 5, done: 3, expected: 10, ok: 2, locked: 2, failed: 2, missing: 1, blocked: 1, unobserved: 4, stale: 2}
	if got != want {
		t.Fatalf("got %+v want %+v", got, want)
	}
	planExec(t, db, `UPDATE game_current_prices SET price_status='ok',attempt_outcome='success',updated_at=?`, timestamp(end.Add(time.Hour)))
	if readDataStats(t, db, 1) != want {
		t.Fatal("frozen columns changed with live prices")
	}
}

func TestDataFreezeChunkingEmptyAndRollback(t *testing.T) {
	db, gate := runtimeFixture(t)
	r := New(db, gate, nil, Options{})
	ctx := context.Background()
	now := time.Date(2026, 11, 6, 12, 0, 0, 0, beijing)
	ids := make([]int64, 601)
	for i := range ids {
		ids[i] = int64(i + 1)
	}
	planExec(t, db, `INSERT INTO price_cycles(id,kind,status,scope,expected_json) VALUES (1,'manual','finalizing','pool',?)`, jsonString(map[string]any{"appids": ids, "regions": []string{"cn"}}))
	dataObservation(t, db, 601, "CN", "ok", "success", "ok", timestamp(now), timestamp(now))
	if err := r.freezeStats(ctx, 1, nil, nil, now, now); err != nil {
		t.Fatal(err)
	}
	if got := readDataStats(t, db, 1); got.expected != 601 || got.unobserved != 600 || got.done != 1 || got.ok != 1 {
		t.Fatal(got)
	}
	planExec(t, db, `INSERT INTO price_cycles(id,kind,status,scope,expected_json) VALUES (2,'manual','finalizing','pool','{"appids":[],"regions":[]}')`)
	if err := r.freezeStats(ctx, 2, ids, []Region{{"cn", "CNY"}}, now, now); err != nil {
		t.Fatal(err)
	}
	if got := readDataStats(t, db, 2); got != (frozenDataStats{}) {
		t.Fatal(got)
	}
	planExec(t, db, `INSERT INTO price_cycles(id,kind,status,scope,expected_json) VALUES (3,'manual','finalizing','pool','bad')`)
	if err := r.freezeStats(ctx, 3, nil, nil, now, now); err == nil {
		t.Fatal("malformed expected set accepted")
	}
	var frozen sql.NullInt64
	if err := db.QueryRow("SELECT units_expected FROM price_cycles WHERE id=3").Scan(&frozen); err != nil || frozen.Valid {
		t.Fatal(frozen, err)
	}
	if err := r.freezeStats(ctx, 999, nil, nil, now, now); err == nil {
		t.Fatal("missing cycle accepted")
	}
}
