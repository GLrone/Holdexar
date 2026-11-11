package priceevents

import (
	"context"
	"database/sql"
	"encoding/json"
	"path/filepath"
	"reflect"
	"sort"
	"sync"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
)

var testStart = time.Date(2026, 11, 6, 11, 0, 0, 0, time.UTC)
var testEnd = testStart.Add(time.Hour)

type fixture struct {
	t        *testing.T
	db       *sql.DB
	detector *Detector
}

func fresh(t *testing.T) *fixture {
	t.Helper()
	path := filepath.Join(t.TempDir(), "events.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err = store.InitializeFreshDatabase(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	return &fixture{t, db, New(db, store.NewWriteGate(db, path))}
}
func (f *fixture) exec(query string, args ...any) {
	f.t.Helper()
	if _, err := f.db.Exec(query, args...); err != nil {
		f.t.Fatal(err)
	}
}
func (f *fixture) cycle(id int64) {
	f.exec(`INSERT INTO price_cycles(id,kind,status,scope,expected_json,started_at,finished_at,notification_scan_json) VALUES(?,'scheduled','completed','pool','{"appids":[620],"regions":["cn"]}',?,?,'{"state":"done","cursor":9}')`, id, stamp(testStart), stamp(testEnd))
}
func (f *fixture) history(app, price, original, sub int64, currency, source string, at time.Time) {
	f.exec(`INSERT INTO game_price_history(appid,region_code,price,original_price,cny_fen,sub_id,currency,source,snapshot_at,discount_percent,is_gold,is_bundle,price_status) VALUES(?,'CN',?,?,?,?,?,?,?,0,0,0,'ok')`, app, price, original, price, sub, currency, source, stamp(at))
}
func (f *fixture) current(app int64, status string, price any, sub int64, currency string, at time.Time) {
	answer := status
	if status == "ok" {
		answer = "ok"
	}
	f.exec(`INSERT OR REPLACE INTO game_current_prices(appid,region_code,price,cny_fen,sub_id,currency,price_status,discount_percent,fail_count,attempt_outcome,steam_answer,updated_at,last_success_at) VALUES(?,'CN',?,?,?,?,?,0,0,'success',?,?,?)`, app, price, price, sub, currency, status, answer, stamp(at), stamp(at))
}
func (f *fixture) priorState(state, kind string) {
	payload, _ := json.Marshal(map[string]any{"status": state})
	f.exec(`INSERT INTO price_events(cycle_id,appid,region_code,event_type,current_json,occurred_at) VALUES(999,620,'CN',?,?,?)`, kind, string(payload), stamp(testStart.Add(-time.Hour)))
}
func (f *fixture) detect(id int64) []Event {
	f.t.Helper()
	events, err := f.detector.Detect(context.Background(), id)
	if err != nil {
		f.t.Fatal(err)
	}
	return events
}
func kinds(events []Event) []string {
	out := []string{}
	for _, e := range events {
		out = append(out, e.Type)
	}
	sort.Strings(out)
	return out
}

func TestAllEventKindsAndCanonicalPersistence(t *testing.T) {
	tests := []struct {
		name  string
		setup func(*fixture)
		want  []string
	}{
		{"drop", func(f *fixture) {
			f.history(620, 50, 200, 1, "CNY", "steam", testStart.Add(-2*time.Hour))
			f.history(620, 200, 200, 1, "CNY", "steam", testStart.Add(-time.Hour))
			f.history(620, 100, 200, 1, "CNY", "steam", testStart.Add(20*time.Minute))
			f.current(620, "ok", 100, 1, "CNY", testStart.Add(30*time.Minute))
		}, []string{PriceDrop}},
		{"increase", func(f *fixture) {
			f.history(620, 100, 200, 1, "CNY", "steam", testStart.Add(-time.Hour))
			f.history(620, 200, 200, 1, "CNY", "steam", testStart.Add(20*time.Minute))
			f.current(620, "ok", 200, 1, "CNY", testStart.Add(30*time.Minute))
		}, []string{PriceIncrease}},
		{"new low", func(f *fixture) {
			f.history(620, 200, 200, 1, "CNY", "steam", testStart.Add(-time.Hour))
			f.history(620, 100, 200, 1, "CNY", "steam", testStart.Add(20*time.Minute))
			f.current(620, "ok", 100, 1, "CNY", testStart.Add(30*time.Minute))
		}, []string{NewHistoricalLow, PriceDrop}},
		{"low match", func(f *fixture) {
			f.history(620, 100, 200, 1, "CNY", "steam", testStart.Add(-2*time.Hour))
			f.history(620, 200, 200, 1, "CNY", "steam", testStart.Add(-time.Hour))
			f.history(620, 100, 200, 1, "CNY", "steam", testStart.Add(20*time.Minute))
			f.current(620, "ok", 100, 1, "CNY", testStart.Add(30*time.Minute))
		}, []string{HistoricalLowMatch, PriceDrop}},
		{"permanent", func(f *fixture) {
			f.history(620, 200, 200, 1, "CNY", "steam", testStart.Add(-time.Hour))
			f.history(620, 200, 300, 1, "CNY", "steam", testStart.Add(20*time.Minute))
			f.current(620, "ok", 200, 1, "CNY", testStart.Add(30*time.Minute))
		}, []string{PermanentPriceChange}},
		{"locked", func(f *fixture) {
			f.history(620, 200, 200, 2, "USD", "steam", testStart.Add(-time.Hour))
			f.current(620, "locked", nil, 0, "", testStart.Add(30*time.Minute))
		}, []string{RegionLocked}},
		{"unlocked", func(f *fixture) {
			f.priorState("locked", RegionLocked)
			f.current(620, "ok", 200, 1, "CNY", testStart.Add(30*time.Minute))
		}, []string{RegionUnlocked}},
		{"unavailable", func(f *fixture) {
			f.priorState("locked", RegionLocked)
			f.current(620, "blocked", nil, 0, "", testStart.Add(30*time.Minute))
		}, []string{PriceUnavailable}},
		{"restored", func(f *fixture) {
			f.priorState("missing", PriceUnavailable)
			f.current(620, "ok", 200, 1, "CNY", testStart.Add(30*time.Minute))
		}, []string{PriceRestored}},
		{"promo", func(f *fixture) {
			f.history(620, 200, 200, 1, "CNY", "steam", testStart.Add(-time.Hour))
			f.history(620, 0, 200, 1, "CNY", "steam", testStart.Add(20*time.Minute))
			f.current(620, "ok", 0, 1, "CNY", testStart.Add(30*time.Minute))
		}, []string{FreePromo}},
		{"removed", func(f *fixture) {
			f.exec(`INSERT INTO games(appid,name,family_sharing,trading_cards,is_epic,is_hb,hl_flag,pp_flag,diff_fen,is_adult,is_visual_novel,removed_strikes,positive_reviews,review_count,view_count,removed_at) VALUES(620,'Portal 2',0,0,0,0,0,0,0,0,0,1,0,0,0,?)`, stamp(testStart.Add(30*time.Minute)))
		}, []string{Removed}},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			f := fresh(t)
			f.cycle(1)
			tc.setup(f)
			got := f.detect(1)
			sort.Strings(tc.want)
			if !reflect.DeepEqual(kinds(got), tc.want) {
				t.Fatalf("events=%+v want=%v", got, tc.want)
			}
			for _, e := range got {
				if e.ID <= 0 || e.CycleID != 1 || e.AppID != 620 || e.OccurredAt == "" || e.CreatedAt == "" {
					t.Fatalf("incomplete event: %+v", e)
				}
				if e.Type == FreePromo || e.Type == Removed {
					if e.Region != nil {
						t.Fatal("game event region must be null")
					}
				} else if e.Region == nil || *e.Region != "CN" {
					t.Fatal("regional identity lost")
				}
			}
			if duplicate := f.detect(1); len(duplicate) != 0 {
				t.Fatalf("replay produced %+v", duplicate)
			}
			var scan string
			f.db.QueryRow(`SELECT notification_scan_json FROM price_cycles WHERE id=1`).Scan(&scan)
			var value map[string]any
			json.Unmarshal([]byte(scan), &value)
			if value["state"] != "pending" || value["cursor"] != float64(9) {
				t.Fatalf("notification scan=%s", scan)
			}
		})
	}
}

func TestEvidenceBoundaries(t *testing.T) {
	tests := []struct{ name, change string }{
		{"unobserved", `DELETE FROM game_current_prices`},
		{"stale", `UPDATE game_current_prices SET updated_at='2026-11-06 09:00:00.000000',last_success_at='2026-11-06 09:00:00.000000'`},
		{"future", `UPDATE game_current_prices SET updated_at='2026-11-06 13:00:00.000000',last_success_at='2026-11-06 13:00:00.000000'`},
		{"failed retained", `UPDATE game_current_prices SET attempt_outcome='failed'`},
		{"retained success time", `UPDATE game_current_prices SET last_success_at='2026-11-06 09:00:00.000000'`},
		{"no options", `UPDATE game_current_prices SET steam_answer='no_options'`},
		{"other region", `UPDATE game_current_prices SET region_code='US'`},
		{"other app", `UPDATE game_current_prices SET appid=621`},
		{"other currency", `UPDATE game_price_history SET currency='USD' WHERE snapshot_at<'2026-11-06 11:00:00'`},
		{"other sub", `UPDATE game_price_history SET sub_id=2 WHERE snapshot_at<'2026-11-06 11:00:00'`},
		{"unknown identity", `UPDATE game_price_history SET product_unknown=1 WHERE snapshot_at<'2026-11-06 11:00:00'`},
		{"gold identity", `UPDATE game_price_history SET is_gold=1 WHERE snapshot_at<'2026-11-06 11:00:00'`},
		{"version identity", `UPDATE game_price_history SET version_suffix='Deluxe' WHERE snapshot_at<'2026-11-06 11:00:00'`},
		{"bundle identity", `UPDATE game_price_history SET is_bundle=1 WHERE snapshot_at<'2026-11-06 11:00:00'`},
		{"community current", `UPDATE game_price_history SET source='community' WHERE snapshot_at>'2026-11-06 11:00:00'`},
		{"snapshot mismatch", `UPDATE game_price_history SET price=99 WHERE snapshot_at>'2026-11-06 11:00:00'`},
		{"snapshot after current", `UPDATE game_price_history SET snapshot_at='2026-11-06 11:59:00.000000' WHERE snapshot_at>'2026-11-06 11:00:00'`},
		{"no snapshot", `DELETE FROM game_price_history WHERE snapshot_at>'2026-11-06 11:00:00'`},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			f := fresh(t)
			f.cycle(1)
			f.history(620, 200, 200, 1, "CNY", "steam", testStart.Add(-time.Hour))
			f.history(620, 100, 200, 1, "CNY", "steam", testStart.Add(20*time.Minute))
			f.current(620, "ok", 100, 1, "CNY", testStart.Add(30*time.Minute))
			f.exec(tc.change)
			if got := f.detect(1); len(got) != 0 {
				t.Fatalf("unexpected events %+v", got)
			}
		})
	}
}

func TestCommunityBaselineRequiresSteamIdentityCorroboration(t *testing.T) {
	for _, corroborated := range []bool{false, true} {
		t.Run(map[bool]string{true: "corroborated", false: "uncorroborated"}[corroborated], func(t *testing.T) {
			f := fresh(t)
			f.cycle(1)
			f.history(620, 200, 200, 1, "CNY", "community", testStart.Add(-time.Hour))
			f.history(620, 100, 200, 1, "CNY", "steam", testStart.Add(20*time.Minute))
			f.current(620, "ok", 100, 1, "CNY", testStart.Add(30*time.Minute))
			if !corroborated {
				f.exec(`UPDATE game_price_history SET price=0 WHERE source='steam'`)
				f.current(620, "ok", 0, 1, "CNY", testStart.Add(30*time.Minute))
			}
			got := f.detect(1)
			if corroborated {
				if !reflect.DeepEqual(kinds(got), []string{NewHistoricalLow, PriceDrop}) {
					t.Fatalf("events=%+v", got)
				}
				if got[0].Previous["source"] != "community" {
					t.Fatal("community evidence lost")
				}
			} else if len(got) != 0 {
				t.Fatalf("uncorroborated events=%+v", got)
			}
		})
	}
}

func TestPersistenceBatchAndRollback(t *testing.T) {
	f := fresh(t)
	f.cycle(1)
	appIDs := make([]int64, 620)
	for i := range appIDs {
		appIDs[i] = int64(i + 1)
	}
	apps, _ := json.Marshal(appIDs)
	f.exec(`INSERT INTO game_price_history(appid,region_code,price,original_price,cny_fen,sub_id,currency,source,snapshot_at,discount_percent,is_gold,is_bundle,price_status) SELECT value,'CN',200,200,200,1,'CNY','steam',?,0,0,0,'ok' FROM json_each(?)`, stamp(testStart.Add(-time.Hour)), string(apps))
	f.exec(`INSERT INTO game_price_history(appid,region_code,price,original_price,cny_fen,sub_id,currency,source,snapshot_at,discount_percent,is_gold,is_bundle,price_status) SELECT value,'CN',100,200,100,1,'CNY','steam',?,0,0,0,'ok' FROM json_each(?)`, stamp(testStart.Add(20*time.Minute)), string(apps))
	f.exec(`INSERT INTO game_current_prices(appid,region_code,price,cny_fen,sub_id,currency,price_status,discount_percent,fail_count,attempt_outcome,steam_answer,updated_at,last_success_at) SELECT value,'CN',100,100,1,'CNY','ok',0,0,'success','ok',?,? FROM json_each(?)`, stamp(testStart.Add(30*time.Minute)), stamp(testStart.Add(30*time.Minute)), string(apps))
	f.exec(`CREATE TRIGGER fail_scan BEFORE UPDATE OF notification_scan_json ON price_cycles BEGIN SELECT RAISE(ABORT,'scan write failure'); END`)
	_, err := f.detector.DetectWindow(context.Background(), appIDs, []string{"CN"}, testStart, testEnd, 1)
	if err == nil {
		t.Fatal("injected failure was swallowed")
	}
	var count int
	if err = f.db.QueryRow(`SELECT COUNT(*) FROM price_events`).Scan(&count); err != nil || count != 0 {
		t.Fatalf("partial transaction count=%d error=%v", count, err)
	}
	f.exec(`DROP TRIGGER fail_scan`)
	got, err := f.detector.DetectWindow(context.Background(), appIDs, []string{"CN"}, testStart, testEnd, 1)
	if err != nil || len(got) != 1240 {
		t.Fatalf("batch events=%d error=%v", len(got), err)
	}
	f.exec(`UPDATE price_cycles SET notification_scan_json='{"state":"scanning","cursor":19}' WHERE id=1`)
	f.current(620, "ok", 100, 1, "CNY", testStart.Add(30*time.Minute))
	if got, err = f.detector.DetectWindow(context.Background(), appIDs, []string{"CN"}, testStart, testEnd, 1); err != nil || len(got) != 0 {
		t.Fatalf("batch replay=%d error=%v", len(got), err)
	}
	var scan string
	if err = f.db.QueryRow(`SELECT notification_scan_json FROM price_cycles WHERE id=1`).Scan(&scan); err != nil || scan != `{"state":"scanning","cursor":19}` {
		t.Fatalf("scan=%s error=%v", scan, err)
	}
}

func TestObservedTimeBaselineAndActualCycleWindow(t *testing.T) {
	f := fresh(t)
	f.cycle(1)
	f.history(620, 200, 200, 1, "CNY", "steam", testStart.Add(10*time.Minute))
	f.history(620, 100, 200, 1, "CNY", "steam", testStart.Add(20*time.Minute))
	f.current(620, "ok", 100, 1, "CNY", testStart.Add(30*time.Minute))
	got := f.detect(1)
	if !reflect.DeepEqual(kinds(got), []string{NewHistoricalLow, PriceDrop}) {
		t.Fatalf("observed-time comparison events=%+v", got)
	}
	if got[0].OccurredAt != "2026-11-06T11:20:00" {
		t.Fatalf("occurrence=%q", got[0].OccurredAt)
	}
	f.exec(`UPDATE price_cycles SET started_at='2026-11-06 11:40:00.000000' WHERE id=1`)
	if got = f.detect(1); len(got) != 0 {
		t.Fatal("out of window observation used")
	}
}

func TestConcurrentReplayAndStandaloneWindow(t *testing.T) {
	f := fresh(t)
	f.history(620, 200, 200, 1, "CNY", "steam", testStart.Add(-time.Hour))
	f.history(620, 100, 200, 1, "CNY", "steam", testStart.Add(20*time.Minute))
	f.current(620, "ok", 100, 1, "CNY", testStart.Add(30*time.Minute))
	var wg sync.WaitGroup
	counts := make(chan int, 4)
	errs := make(chan error, 4)
	for range 4 {
		wg.Go(func() {
			rows, err := f.detector.DetectWindow(context.Background(), []int64{620}, []string{" cn "}, testStart, testEnd, -42)
			counts <- len(rows)
			errs <- err
		})
	}
	wg.Wait()
	close(counts)
	close(errs)
	total := 0
	for n := range counts {
		total += n
	}
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	if total != 2 {
		t.Fatalf("new event count=%d", total)
	}
	var count int
	f.db.QueryRow(`SELECT COUNT(*) FROM price_events WHERE cycle_id=-42`).Scan(&count)
	if count != 2 {
		t.Fatalf("persisted=%d", count)
	}
	if got := f.detect(999); len(got) != 0 {
		t.Fatal("unknown cycle emitted events")
	}
}

func TestStatusBaselineAndPromoPreference(t *testing.T) {
	t.Run("first observation", func(t *testing.T) {
		f := fresh(t)
		f.cycle(1)
		f.current(620, "locked", nil, 0, "", testStart.Add(30*time.Minute))
		if got := f.detect(1); len(got) != 0 {
			t.Fatal(got)
		}
		f.history(620, 200, 200, 1, "CNY", "steam", testEnd.Add(time.Hour))
		if got := f.detect(1); len(got) != 0 {
			t.Fatal("future history established past status")
		}
	})
	t.Run("latest status", func(t *testing.T) {
		f := fresh(t)
		f.cycle(1)
		f.priorState("locked", RegionLocked)
		f.priorState("missing", PriceUnavailable)
		f.current(620, "ok", 200, 1, "CNY", testStart.Add(30*time.Minute))
		if got := kinds(f.detect(1)); !reflect.DeepEqual(got, []string{PriceRestored}) {
			t.Fatal(got)
		}
	})
	t.Run("CN promo preferred", func(t *testing.T) {
		f := fresh(t)
		f.cycle(1)
		f.history(620, 200, 200, 1, "CNY", "steam", testStart.Add(-time.Hour))
		f.history(620, 0, 200, 1, "CNY", "steam", testStart.Add(20*time.Minute))
		f.current(620, "ok", 0, 1, "CNY", testStart.Add(30*time.Minute))
		f.exec(`INSERT INTO game_price_history(appid,region_code,price,original_price,cny_fen,sub_id,currency,source,snapshot_at,discount_percent,is_gold,is_bundle,price_status) SELECT appid,'US',price,original_price,cny_fen,sub_id,'USD',source,snapshot_at,discount_percent,is_gold,is_bundle,price_status FROM game_price_history`)
		f.exec(`INSERT INTO game_current_prices SELECT appid,'US','USD',price,original_price,discount_percent,sub_id,price_status,fail_count,cny_fen,discount_end_ts,attempt_outcome,steam_answer,last_success_at,updated_at FROM game_current_prices`)
		events, err := f.detector.DetectWindow(context.Background(), []int64{620}, []string{"US", "CN"}, testStart, testEnd, 1)
		if err != nil {
			t.Fatal(err)
		}
		if len(events) != 1 || events[0].Type != FreePromo || events[0].Current["region"] != "CN" {
			t.Fatal(events)
		}
	})
}
