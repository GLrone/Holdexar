package steamevents

import (
	"database/sql"
	"encoding/json"
	"path/filepath"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
)

const windowsWatermarkSetting = "steam_events.backfill_watermark"

func newWindowsTest(t *testing.T) (*sql.DB, *store.WriteGate, *Windows) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "windows.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err := store.InitializeFreshDatabase(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	gate := store.NewWriteGate(db, path)
	now := func() time.Time { return time.Date(2026, 11, 9, 4, 0, 0, 0, time.UTC) }
	return db, gate, NewWindows(db, gate, now)
}

func execWindowsTest(t *testing.T, db *sql.DB, query string, args ...any) {
	t.Helper()
	if _, err := db.ExecContext(t.Context(), query, args...); err != nil {
		t.Fatal(err)
	}
}

func addWindowsPrice(t *testing.T, db *sql.DB, id int, snapshot string, key any) {
	t.Helper()
	execWindowsTest(t, db, `INSERT INTO game_price_history
		(appid, region_code, price, discount_percent, is_gold, is_bundle, price_status, snapshot_at, steam_event_key)
		VALUES (?, 'CN', 100, 0, 0, 0, 'normal', ?, ?)`, id, snapshot, key)
}

func requireWindowsTag(t *testing.T, db *sql.DB, id int, want sql.NullString) {
	t.Helper()
	var got sql.NullString
	if err := db.QueryRowContext(t.Context(), `SELECT steam_event_key FROM game_price_history WHERE appid = ?`, id).Scan(&got); err != nil {
		t.Fatal(err)
	}
	if got != want {
		t.Fatalf("observation %d tag = %v, want %v", id, got, want)
	}
}

func windowsTestMarks(t *testing.T, db *sql.DB) map[string]string {
	t.Helper()
	var raw string
	if err := db.QueryRowContext(t.Context(), `SELECT value_json FROM app_settings WHERE key = ?`, windowsWatermarkSetting).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	var marks map[string]string
	if err := json.Unmarshal([]byte(raw), &marks); err != nil {
		t.Fatal(err)
	}
	return marks
}

func TestWindowsBackfillUsesLivePriorityAndBeijingDay(t *testing.T) {
	db, _, windows := newWindowsTest(t)
	execWindowsTest(t, db, `INSERT INTO steam_events (event_key, category, name_en, start_date, end_date) VALUES
		('themed', 'themed_fest', 'Themed', '2026-11-04', '2026-11-07'),
		('next', 'next_fest', 'Next', '2026-11-05', '2026-11-07'),
		('season', 'seasonal_sale', 'Season', '2026-11-06', '2026-11-07')`)
	addWindowsPrice(t, db, 1, "2026-11-05 23:59:59.999999", nil)
	addWindowsPrice(t, db, 2, "2026-11-06 00:00:00.000000", nil)
	addWindowsPrice(t, db, 3, "2026-11-07 23:59:59.999999", nil)
	addWindowsPrice(t, db, 4, "2026-11-08 00:00:00.000000", nil)
	addWindowsPrice(t, db, 5, "2026-11-06 00:00:01.000000", "previous")
	if err := windows.Reload(t.Context()); err != nil {
		t.Fatal(err)
	}
	beijingMidnight := time.Date(2026, 11, 5, 16, 0, 0, 0, time.UTC)
	if got := windows.KeyAt(beijingMidnight); got == nil || *got != "season" {
		t.Fatalf("Beijing midnight did not select season: %v", got)
	}
	if got := windows.KeyAt(beijingMidnight.Add(-time.Nanosecond)); got == nil || *got != "next" {
		t.Fatalf("before Beijing midnight did not select next fest: %v", got)
	}
	if got := windows.KeyAt(beijingMidnight.AddDate(0, 0, 2)); got != nil {
		t.Fatalf("after inclusive end date selected %q", *got)
	}
	if count, err := windows.Backfill(t.Context(), []string{"themed", "next"}); err != nil || count != 1 {
		t.Fatalf("backfill with higher-priority event excluded = %d, %v", count, err)
	}
	requireWindowsTag(t, db, 1, sql.NullString{String: "next", Valid: true})
	requireWindowsTag(t, db, 2, sql.NullString{})
	if count, err := windows.Backfill(t.Context(), []string{"themed", "next", "season"}); err != nil || count != 2 {
		t.Fatalf("backfill = %d, %v", count, err)
	}
	requireWindowsTag(t, db, 2, sql.NullString{String: "season", Valid: true})
	requireWindowsTag(t, db, 3, sql.NullString{String: "season", Valid: true})
	requireWindowsTag(t, db, 4, sql.NullString{})
	requireWindowsTag(t, db, 5, sql.NullString{String: "previous", Valid: true})
}

func TestWindowsInvalidatesLegacyMarkOnCalendarRevision(t *testing.T) {
	db, gate, windows := newWindowsTest(t)
	execWindowsTest(t, db, `INSERT INTO steam_events (event_key, category, name_en, start_date, end_date)
		VALUES ('season', 'seasonal_sale', 'Season', '2026-11-06', '2026-11-07')`)
	execWindowsTest(t, db, `INSERT INTO app_settings (key, value_json)
		VALUES (?, '{"season": "2026-11-07 12:00:00.123456", "other": "2026-11-01 12:00:00"}')`, windowsWatermarkSetting)
	addWindowsPrice(t, db, 1, "2026-11-04 12:00:00.000000", nil)
	addWindowsPrice(t, db, 2, "2026-11-07 12:00:00.123455", nil)
	addWindowsPrice(t, db, 3, "2026-11-07 12:00:00.123456", nil)
	if err := windows.Reload(t.Context()); err != nil {
		t.Fatal(err)
	}
	if count, err := windows.Backfill(t.Context(), []string{"season"}); err != nil || count != 1 {
		t.Fatalf("backfill with Python mark = %d, %v", count, err)
	}
	requireWindowsTag(t, db, 2, sql.NullString{})
	if err := gate.WithWrite(t.Context(), store.PriorityBackground, func(conn *sql.Conn) error {
		if err := InvalidateBackfill(t.Context(), conn, []string{"season"}); err != nil {
			return err
		}
		_, err := conn.ExecContext(t.Context(), `UPDATE steam_events SET start_date = '2026-11-04' WHERE event_key = 'season'`)
		return err
	}); err != nil {
		t.Fatal(err)
	}
	if err := windows.Reload(t.Context()); err != nil {
		t.Fatal(err)
	}
	if count, err := windows.Backfill(t.Context(), []string{"season"}); err != nil || count != 2 {
		t.Fatalf("backfill after revision = %d, %v", count, err)
	}
	requireWindowsTag(t, db, 1, sql.NullString{String: "season", Valid: true})
	marks := windowsTestMarks(t, db)
	if marks["season"] != "2026-11-09 11:00:00.000000" || marks["other"] != "2026-11-01 12:00:00" {
		t.Fatalf("stored marks = %v", marks)
	}
}

func TestWindowsCalendarRevisionExposesPreviouslyOverlappedHistory(t *testing.T) {
	db, gate, windows := newWindowsTest(t)
	execWindowsTest(t, db, `INSERT INTO steam_events (event_key, category, name_en, start_date, end_date) VALUES
		('season', 'seasonal_sale', 'Season', '2026-11-05', '2026-11-06'),
		('next', 'next_fest', 'Next', '2026-11-06', '2026-11-07')`)
	if err := windows.Reload(t.Context()); err != nil {
		t.Fatal(err)
	}
	if count, err := windows.Backfill(t.Context(), []string{"next"}); err != nil || count != 0 {
		t.Fatalf("advance overlapping event's mark = %d, %v", count, err)
	}
	addWindowsPrice(t, db, 1, "2026-11-06 12:00:00.000000", nil)
	if err := gate.WithWrite(t.Context(), store.PriorityBackground, func(conn *sql.Conn) error {
		if err := InvalidateBackfill(t.Context(), conn, []string{"season"}); err != nil {
			return err
		}
		_, err := conn.ExecContext(t.Context(), `UPDATE steam_events SET end_date = '2026-11-05' WHERE event_key = 'season'`)
		return err
	}); err != nil {
		t.Fatal(err)
	}
	if err := windows.Reload(t.Context()); err != nil {
		t.Fatal(err)
	}
	if count, err := windows.Backfill(t.Context(), []string{"next", "season"}); err != nil || count != 1 {
		t.Fatalf("backfill after removing overlap = %d, %v", count, err)
	}
	requireWindowsTag(t, db, 1, sql.NullString{String: "next", Valid: true})
}

func TestWindowsRetriesOnlyUncommittedBackfill(t *testing.T) {
	db, _, windows := newWindowsTest(t)
	execWindowsTest(t, db, `INSERT INTO steam_events (event_key, category, name_en, start_date, end_date) VALUES
		('season', 'seasonal_sale', 'Season', '2026-11-05', '2026-11-05'),
		('next', 'next_fest', 'Next', '2026-11-06', '2026-11-06')`)
	addWindowsPrice(t, db, 1, "2026-11-05 12:00:00.000000", nil)
	addWindowsPrice(t, db, 2, "2026-11-06 12:00:00.000000", nil)
	execWindowsTest(t, db, `CREATE TRIGGER fail_next_watermark BEFORE UPDATE ON app_settings
		WHEN json_extract(NEW.value_json, '$.next') IS NOT NULL
		BEGIN SELECT RAISE(ABORT, 'failed watermark'); END`)
	if err := windows.Reload(t.Context()); err != nil {
		t.Fatal(err)
	}
	keys := []string{"next", "season"}
	if count, err := windows.Backfill(t.Context(), keys); err == nil || count != 1 {
		t.Fatalf("partial backfill = %d, %v", count, err)
	}
	requireWindowsTag(t, db, 1, sql.NullString{String: "season", Valid: true})
	requireWindowsTag(t, db, 2, sql.NullString{})
	if marks := windowsTestMarks(t, db); len(marks) != 1 || marks["season"] == "" {
		t.Fatalf("marks after failed transaction = %v", marks)
	}
	execWindowsTest(t, db, `DROP TRIGGER fail_next_watermark`)
	if count, err := windows.Backfill(t.Context(), keys); err != nil || count != 1 {
		t.Fatalf("retried backfill = %d, %v", count, err)
	}
	requireWindowsTag(t, db, 2, sql.NullString{String: "next", Valid: true})
}
