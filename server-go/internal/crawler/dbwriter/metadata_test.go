package dbwriter

import (
	"context"
	"database/sql"
	"errors"
	"reflect"
	"testing"
	"time"
)

func metadataExec(t *testing.T, db *sql.DB, q string, args ...any) {
	t.Helper()
	if _, err := db.Exec(q, args...); err != nil {
		t.Fatal(err)
	}
}

func TestMetadataClearOnlySelectedMissingAndRecompute(t *testing.T) {
	w, db := newTestWriter(t)
	w.Now = testNow
	w.FxRates = map[string]float64{"USD": 1}
	runBatch(t, w, []BatchEntry{{Game: gameData(1, "Known"), Prices: []PriceRow{priceRow(1, "CN", 1000), priceRow(1, "US", 800), priceRow(1, "GB", 600)}}})
	metadataExec(t, db, `UPDATE game_current_prices SET price_status=CASE region_code WHEN 'US' THEN 'missing' WHEN 'GB' THEN 'blocked' ELSE 'ok' END`)
	metadataExec(t, db, `INSERT INTO game_current_prices(appid,region_code,price_status,discount_percent,fail_count) VALUES (1,'AU','missing',0,0)`)
	metadataExec(t, db, `UPDATE games SET min_cny_fen=999 WHERE appid=1`)
	history := dumpHistory(t, db)
	count, err := w.ClearMissingRegionsBatch(context.Background(), []RegionPair{{1, "us"}, {1, "US"}, {1, "CN"}, {1, "GB"}})
	if err != nil || count != 1 {
		t.Fatalf("clear=%d err=%v", count, err)
	}
	rows := dumpCurrent(t, db)
	if len(rows) != 3 || rows[0]["region_code"] != "AU" || rows[1]["region_code"] != "CN" || rows[2]["region_code"] != "GB" {
		t.Fatalf("remaining=%v", rows)
	}
	if rows[1]["price_status"] != "ok" || rows[1]["price"] != int64(1000) || rows[2]["price_status"] != "blocked" {
		t.Fatal("non-missing price changed")
	}
	if got := dumpGameCol(t, db, 1, "min_cny_fen"); got != nil {
		t.Fatalf("derived minimum not cleared: %v", got)
	}
	if !reflect.DeepEqual(history, dumpHistory(t, db)) {
		t.Fatal("history changed")
	}
}

func TestMetadataClearDerivedFailureRollsBackDeletion(t *testing.T) {
	w, db := newTestWriter(t)
	w.Now = testNow
	runBatch(t, w, []BatchEntry{{Game: gameData(1, "Known"), Prices: []PriceRow{priceRow(1, "US", 800)}}})
	metadataExec(t, db, `UPDATE game_current_prices SET price_status='missing'`)
	metadataExec(t, db, `CREATE TRIGGER reject_derived BEFORE UPDATE OF hl_flag ON games BEGIN SELECT RAISE(ABORT,'derived failure'); END`)
	before := dumpCurrent(t, db)
	count, err := w.ClearMissingRegionsBatch(context.Background(), []RegionPair{{1, "us"}})
	var statusErr *RegionStatusWriteError
	if count != 0 || !errors.As(err, &statusErr) || !reflect.DeepEqual(statusErr.Pairs, []RegionPair{{1, "US"}}) {
		t.Fatalf("rollback result=%d %v", count, err)
	}
	if !reflect.DeepEqual(before, dumpCurrent(t, db)) {
		t.Fatal("failed derived maintenance left partial deletion")
	}
}

func TestMetadataMarkTypePreservesKnownAndCreatesUnknown(t *testing.T) {
	w, db := newTestWriter(t)
	w.Now = testNow
	runBatch(t, w, []BatchEntry{{Game: gameData(1, "Known title"), Prices: []PriceRow{priceRow(1, "CN", 1000)}}})
	metadataExec(t, db, `UPDATE games SET xgp_tier='ultimate',series_id='old-series' WHERE appid=1`)
	before := dumpRows(t, db, `SELECT name,CAST(created_at AS TEXT) created_at,xgp_tier FROM games WHERE appid=1`)
	history := dumpHistory(t, db)
	next := testNow().Add(time.Hour)
	w.Now = func() time.Time { return next }
	if err := w.MarkNonGameTypesBatch(context.Background(), map[int64]string{1: "demo", 2: "music", 3: ""}); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(before, dumpRows(t, db, `SELECT name,CAST(created_at AS TEXT) created_at,xgp_tier FROM games WHERE appid=1`)) {
		t.Fatal("known metadata overwritten")
	}
	if got := dumpGameCol(t, db, 1, "type"); got != "DEMO" {
		t.Fatalf("known type=%v", got)
	}
	if got := dumpGameCol(t, db, 2, "name"); got != "AppID_2" {
		t.Fatalf("placeholder name=%v", got)
	}
	if got := dumpGameCol(t, db, 2, "type"); got != "MUSIC" {
		t.Fatalf("placeholder type=%v", got)
	}
	if got := dumpGameCol(t, db, 3, "type"); got != nil {
		t.Fatalf("empty type=%v", got)
	}
	rows := dumpRows(t, db, `SELECT CAST(updated_at AS TEXT) updated_at FROM games ORDER BY appid`)
	for _, row := range rows {
		if row["updated_at"] != FormatNaive(next) {
			t.Fatalf("updated_at=%v", row)
		}
	}
	if got := dumpGameCol(t, db, 1, "series_id"); got == "old-series" {
		t.Fatal("type catalog change did not update series")
	}
	if !reflect.DeepEqual(history, dumpHistory(t, db)) {
		t.Fatal("type metadata appended history")
	}
}

func TestMetadataMarkTypeDerivedFailureRollsBack(t *testing.T) {
	w, db := newTestWriter(t)
	w.Now = testNow
	runBatch(t, w, []BatchEntry{{Game: gameData(1, "Known")}})
	metadataExec(t, db, `CREATE TRIGGER reject_derived BEFORE UPDATE OF hl_flag ON games BEGIN SELECT RAISE(ABORT,'derived failure'); END`)
	if err := w.MarkNonGameTypesBatch(context.Background(), map[int64]string{1: "demo", 2: "music"}); err == nil {
		t.Fatal("derived failure swallowed")
	}
	if got := dumpGameCol(t, db, 1, "type"); got != "GAME" {
		t.Fatalf("known type partially written: %v", got)
	}
	var count int
	if err := db.QueryRow(`SELECT count(*) FROM games WHERE appid=2`).Scan(&count); err != nil || count != 0 {
		t.Fatalf("unknown insertion survived rollback: %d %v", count, err)
	}
}

func TestMetadataReplaceTagsPresenceAndRollback(t *testing.T) {
	w, db := newTestWriter(t)
	metadataExec(t, db, `CREATE TABLE game_tags(appid INTEGER NOT NULL,tagid INTEGER NOT NULL,weight INTEGER NOT NULL,PRIMARY KEY(appid,tagid))`)
	metadataExec(t, db, `INSERT INTO game_tags VALUES(1,1,10),(2,2,20),(3,3,30),(4,4,40)`)
	if err := w.UpsertTagsBatch(context.Background(), map[int64][][2]int64{1: {{8, 80}, {8, 800}, {9, 90}}, 2: nil, 4: {}}); err != nil {
		t.Fatal(err)
	}
	got := dumpRows(t, db, `SELECT appid,tagid,weight FROM game_tags ORDER BY appid,tagid`)
	want := []map[string]any{{"appid": int64(1), "tagid": int64(8), "weight": int64(80)}, {"appid": int64(1), "tagid": int64(9), "weight": int64(90)}, {"appid": int64(3), "tagid": int64(3), "weight": int64(30)}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("tags=%v", got)
	}
	metadataExec(t, db, `CREATE TRIGGER reject_tag BEFORE INSERT ON game_tags WHEN NEW.tagid=99 BEGIN SELECT RAISE(ABORT,'tag failure'); END`)
	if err := w.UpsertTagsBatch(context.Background(), map[int64][][2]int64{1: {{99, 1}}, 3: nil}); err == nil {
		t.Fatal("tag insertion failure swallowed")
	}
	if !reflect.DeepEqual(got, dumpRows(t, db, `SELECT appid,tagid,weight FROM game_tags ORDER BY appid,tagid`)) {
		t.Fatal("failed replacement lost old tags")
	}
}

func TestMarkComingSoonCreatesPlaceholderRow(t *testing.T) {
	w, db := newTestWriter(t)
	w.Now = testNow
	if err := w.MarkComingSoon(context.Background(), 990101); err != nil {
		t.Fatal(err)
	}
	var name, typ, updatedAt string
	if err := db.QueryRow(`SELECT name, type, CAST(updated_at AS TEXT) FROM games WHERE appid=990101`).Scan(&name, &typ, &updatedAt); err != nil {
		t.Fatal(err)
	}
	if name != "AppID_990101" || typ != "COMING_SOON" || updatedAt != "2026-10-31 12:30:45.123456" {
		t.Fatalf("挂名行不符: %q %q %q", name, typ, updatedAt)
	}
}

func TestMarkComingSoonConflictKeepsNameAndClearsRemoved(t *testing.T) {
	w, db := newTestWriter(t)
	w.Now = testNow
	metadataExec(t, db, `INSERT INTO games (appid, name, family_sharing, trading_cards, is_epic, is_hb, hl_flag, pp_flag, diff_fen, is_adult, is_visual_novel, removed_strikes, positive_reviews, review_count, view_count, removed_at, created_at)
		VALUES (990102, '真名游戏', 0, 0, 0, 0, 0, 0, 0, 0, 0, 2, 0, 0, 0, '2026-01-01 00:00:00', '2025-01-01 00:00:00')`)
	if err := w.MarkComingSoon(context.Background(), 990102); err != nil {
		t.Fatal(err)
	}
	var name, typ, removedAt, createdAt any
	var removedStrikes int64
	if err := db.QueryRow(`SELECT name, type, removed_at, removed_strikes, CAST(created_at AS TEXT) FROM games WHERE appid=990102`).Scan(&name, &typ, &removedAt, &removedStrikes, &createdAt); err != nil {
		t.Fatal(err)
	}
	if name != "真名游戏" || typ != "COMING_SOON" {
		t.Fatalf("冲突覆盖不符: name=%v type=%v", name, typ)
	}
	if removedAt != nil || removedStrikes != 0 {
		t.Fatalf("复活痕迹应一并清除: %v %v", removedAt, removedStrikes)
	}
	if createdAt != "2025-01-01 00:00:00" {
		t.Fatalf("created_at 冲突应保留原值: %v", createdAt)
	}
}

func TestRecordBundleDiscoveries(t *testing.T) {
	w, db := newTestWriter(t)
	w.Now = testNow
	ctx := context.Background()
	if n, err := w.RecordBundleDiscoveries(ctx, nil); err != nil || n != 0 {
		t.Fatalf("空输入应 0: %d %v", n, err)
	}
	metadataExec(t, db, `INSERT INTO bundles (bundle_id, name, must_purchase_as_set, item_kind, is_lowest, diff_fen, app_ids, view_count, updated_at)
		VALUES (111, '既有完整主档', 0, 0, 0, 0, '[220,440]', 5, '2026-01-01 00:00:00')`)
	discoveries := []BundleDiscovery{
		{BundleID: 111, Name: "已知包", MPS: 1, ItemKind: 0, AppIDs: []int64{220}},
		{BundleID: 222, Name: "新发现包", MPS: 0, ItemKind: 0, AppIDs: []int64{220}},
	}
	n, err := w.RecordBundleDiscoveries(ctx, discoveries)
	if err != nil || n != 1 {
		t.Fatalf("新插数应为 1: %d %v", n, err)
	}
	var name, appIDs, updatedAt any
	var mps, itemKind, viewCount int64
	if err := db.QueryRow(`SELECT name, app_ids, updated_at, must_purchase_as_set, item_kind, view_count FROM bundles WHERE bundle_id=222`).Scan(&name, &appIDs, &updatedAt, &mps, &itemKind, &viewCount); err != nil {
		t.Fatal(err)
	}
	if name != "新发现包" || appIDs != "[220]" || updatedAt != nil || mps != 0 || itemKind != 0 || viewCount != 0 {
		t.Fatalf("发现桩形状不符: %v %v %v %v %v %v", name, appIDs, updatedAt, mps, itemKind, viewCount)
	}
	if n, err = w.RecordBundleDiscoveries(ctx, discoveries); err != nil || n != 0 {
		t.Fatalf("冲突幂等应 0: %d %v", n, err)
	}
	if _, err := db.Exec(`SELECT 1`); err != nil {
		t.Fatal(err)
	}
	var rowCount int
	if err := db.QueryRow(`SELECT COUNT(*) FROM bundles`).Scan(&rowCount); err != nil || rowCount != 2 {
		t.Fatalf("不应新增行: %d %v", rowCount, err)
	}
}
