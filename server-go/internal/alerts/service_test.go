package alerts

import (
	"context"
	"database/sql"
	"encoding/json"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
)

func fixture(t *testing.T) (*Service, *sql.DB) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, e := store.Open(path)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { db.Close() })
	if e = store.InitializeFreshDatabase(context.Background(), db); e != nil {
		t.Fatal(e)
	}
	gate := store.NewWriteGate(db, path)
	return New(db, gate, Options{Now: func() time.Time { return time.Date(2026, 11, 6, 11, 0, 0, 0, time.UTC) }}), db
}
func observed(t *testing.T, db *sql.DB) {
	t.Helper()
	_, e := db.Exec(`INSERT INTO games(appid,name,family_sharing,trading_cards,is_epic,is_hb,hl_flag,pp_flag,diff_fen,is_adult,is_visual_novel,removed_strikes,positive_reviews,review_count,view_count) VALUES(620,'传送门2',0,0,0,0,0,0,0,0,0,0,0,0,0);
 INSERT INTO game_price_history(appid,region_code,currency,price,discount_percent,sub_id,is_gold,is_bundle,price_status,source,snapshot_at) VALUES(620,'US','USD',999,50,400,0,0,'ok','steam','2026-11-06 10:00:00.000000');
 INSERT INTO game_current_prices(appid,region_code,currency,price,cny_fen,discount_percent,sub_id,price_status,fail_count,attempt_outcome,steam_answer,last_success_at,updated_at) VALUES(620,'US','USD',999,7000,50,400,'ok',0,'success','ok','2026-11-06 10:00:00.000000','2026-11-06 10:00:00.000000');
 INSERT INTO app_settings(key,value_json) VALUES('notifications.prefs','{"enabled":true}'),('smtp.to_addr','"capture@localhost"')`)
	if e != nil {
		t.Fatal(e)
	}
}
func TestAddImmediatelyEvaluatesAndCommitsRuleFactAndCandidate(t *testing.T) {
	s, db := fixture(t)
	observed(t, db)
	ctx := context.Background()
	got, e := s.Add(ctx, map[string]any{"appid": 620, "region": "us", "targetType": "price", "targetValue": 999, "config": map[string]any{"currency": "USD"}})
	if e != nil {
		t.Fatal(e)
	}
	if got["active"] != false || got["lastTriggeredAt"] == nil {
		t.Fatalf("%#v", got)
	}
	var events, candidates int
	db.QueryRow(`SELECT COUNT(*) FROM alert_events`).Scan(&events)
	db.QueryRow(`SELECT COUNT(*) FROM notification_candidates WHERE source_kind='alert_event' AND status='pending'`).Scan(&candidates)
	if events != 1 || candidates != 1 {
		t.Fatalf("events=%d candidates=%d", events, candidates)
	}
	var wg sync.WaitGroup
	for i := 0; i < 3; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, e := s.CheckAppIDs(ctx, []int64{620}); e != nil {
				t.Error(e)
			}
		}()
	}
	wg.Wait()
	db.QueryRow(`SELECT COUNT(*) FROM alert_events`).Scan(&events)
	if events != 1 {
		t.Fatal("once rule duplicated")
	}
	if _, e = s.DeleteEvent(ctx, 1); e == nil {
		t.Fatal("deleted notification evidence")
	}
}
func TestRepeatedObservationDoesNotConsumeCooldownOrDuplicate(t *testing.T) {
	s, db := fixture(t)
	observed(t, db)
	ctx := context.Background()
	_, e := s.Add(ctx, map[string]any{"appid": 620, "region": "US", "targetType": "pct", "targetValue": 50, "repeatMode": "always"})
	if e != nil {
		t.Fatal(e)
	}
	if _, e = s.CheckAppIDs(ctx, []int64{620}); e != nil {
		t.Fatal(e)
	}
	var n int
	db.QueryRow(`SELECT COUNT(*) FROM alert_events`).Scan(&n)
	if n != 1 {
		t.Fatal(n)
	}
	if _, e = s.Update(ctx, 1, map[string]any{"active": false}); e != nil {
		t.Fatal(e)
	}
	var status, reason string
	db.QueryRow(`SELECT status,reason FROM notification_candidates`).Scan(&status, &reason)
	if status != "suppressed" || reason != "rule_paused" {
		t.Fatalf("%s %s", status, reason)
	}
	_, e = db.Exec(`CREATE TRIGGER reject_candidate BEFORE INSERT ON notification_candidates BEGIN SELECT RAISE(ABORT,'candidate rejected'); END`)
	if e != nil {
		t.Fatal(e)
	}
	_, e = s.Add(ctx, map[string]any{"appid": 620, "region": "US", "targetType": "pct", "targetValue": 10})
	if e == nil {
		t.Fatal("failed candidate transaction succeeded")
	}
	db.QueryRow(`SELECT COUNT(*) FROM price_alerts`).Scan(&n)
	if n != 1 {
		t.Fatalf("partial rule committed: %d", n)
	}
}

func TestBatchEvaluationAndIdempotentReplay(t *testing.T) {
	s, db := fixture(t)
	ctx := context.Background()
	ids := make([]int64, 650)
	for i := range ids {
		ids[i] = int64(1000 + i)
	}
	encoded, _ := json.Marshal(ids)
	for _, statement := range []string{
		`INSERT INTO games(appid,name,family_sharing,trading_cards,is_epic,is_hb,hl_flag,pp_flag,diff_fen,is_adult,is_visual_novel,removed_strikes,positive_reviews,review_count,view_count) SELECT value,'batch',0,0,0,0,0,0,0,0,0,0,0,0,0 FROM json_each(?)`,
		`INSERT INTO game_price_history(appid,region_code,currency,price,discount_percent,sub_id,is_gold,is_bundle,price_status,source,snapshot_at) SELECT value,'US','USD',999,50,400,0,0,'ok','steam','2026-11-06 10:00:00.000000' FROM json_each(?)`,
		`INSERT INTO game_current_prices(appid,region_code,currency,price,discount_percent,sub_id,price_status,fail_count,attempt_outcome,steam_answer,last_success_at,updated_at) SELECT value,'US','USD',999,50,400,'ok',0,'success','ok','2026-11-06 10:00:00.000000','2026-11-06 10:00:00.000000' FROM json_each(?)`,
		`INSERT INTO price_alerts(appid,region,target_type,target_value,active,repeat_mode,repeat_hours) SELECT value,'US','pct',50,1,'always',24 FROM json_each(?)`,
	} {
		if _, e := db.Exec(statement, string(encoded)); e != nil {
			t.Fatal(e)
		}
	}
	if _, e := db.Exec(`INSERT INTO app_settings(key,value_json) VALUES('notifications.prefs','{"enabled":true}')`); e != nil {
		t.Fatal(e)
	}
	for i := 0; i < 2; i++ {
		n, e := s.CheckAppIDs(ctx, ids)
		if e != nil {
			t.Fatal(e)
		}
		expected := 650
		if i == 1 {
			expected = 0
		}
		if n != expected {
			t.Fatalf("pass %d got %d", i, n)
		}
	}
	var n int
	if e := db.QueryRow(`SELECT COUNT(*) FROM notification_candidates`).Scan(&n); e != nil || n != 650 {
		t.Fatalf("%d %v", n, e)
	}
}

func TestCooldownUsesElapsedHoursWithoutDurationOverflow(t *testing.T) {
	s, db := fixture(t)
	observed(t, db)
	ctx := context.Background()
	_, e := s.Add(ctx, map[string]any{"appid": 620, "region": "US", "targetType": "pct", "targetValue": 50, "repeatMode": "cooldown", "repeatHours": 1000000000})
	if e != nil {
		t.Fatal(e)
	}
	_, e = db.Exec(`UPDATE game_current_prices SET updated_at='2026-11-06 10:01:00.000000',last_success_at='2026-11-06 10:01:00.000000';INSERT INTO game_price_history(appid,region_code,currency,price,discount_percent,sub_id,is_gold,is_bundle,price_status,source,snapshot_at) VALUES(620,'US','USD',999,50,400,0,0,'ok','steam','2026-11-06 10:01:00.000000')`)
	if e != nil {
		t.Fatal(e)
	}
	if n, e := s.CheckAppIDs(ctx, []int64{620}); e != nil || n != 0 {
		t.Fatalf("cooldown %d %v", n, e)
	}
}

func TestDisabledNotificationKeepsRuleAndPreviewNeverWrites(t *testing.T) {
	s, db := fixture(t)
	observed(t, db)
	ctx := context.Background()
	if _, e := db.Exec(`UPDATE app_settings SET value_json='{"enabled":false}' WHERE key='notifications.prefs'`); e != nil {
		t.Fatal(e)
	}
	draft := map[string]any{"appid": 620, "region": "US", "targetType": "pct", "targetValue": 50}
	preview, e := s.Preview(ctx, draft)
	if e != nil || preview["matched"] != true {
		t.Fatalf("%v %v", preview, e)
	}
	var n int
	db.QueryRow(`SELECT COUNT(*) FROM alert_events`).Scan(&n)
	if n != 0 {
		t.Fatal("preview wrote event")
	}
	rule, e := s.Add(ctx, draft)
	if e != nil || rule["active"] != true {
		t.Fatalf("%v %v", rule, e)
	}
	if _, e = db.Exec(`UPDATE app_settings SET value_json='{"enabled":true,"categories":{"rules":false}}' WHERE key='notifications.prefs'`); e != nil {
		t.Fatal(e)
	}
	if n, e := s.CheckAppIDs(ctx, []int64{620}); n != 0 || e != nil {
		t.Fatalf("disabled category %d %v", n, e)
	}
	if _, e = db.Exec(`UPDATE app_settings SET value_json='{"enabled":true}' WHERE key='notifications.prefs'`); e != nil {
		t.Fatal(e)
	}
	cancelled, cancel := context.WithCancel(ctx)
	cancel()
	if _, e = s.CheckAppIDs(cancelled, []int64{620}); e == nil {
		t.Fatal("cancelled mutation accepted")
	}
	if n, e := s.CheckAppIDs(ctx, []int64{620}); n != 1 || e != nil {
		t.Fatalf("enabled rule %d %v", n, e)
	}
}

func TestConcurrentChecksConsumeActiveOnceRuleExactlyOnce(t *testing.T) {
	s, db := fixture(t)
	ctx := context.Background()
	rule, e := s.Add(ctx, map[string]any{"appid": 620, "region": "US", "targetType": "pct", "targetValue": 50})
	if e != nil || rule["active"] != true {
		t.Fatalf("%v %v", rule, e)
	}
	observed(t, db)
	results := make(chan int, 3)
	var wg sync.WaitGroup
	for i := 0; i < 3; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			n, e := s.CheckAppIDs(ctx, []int64{620})
			if e != nil {
				t.Error(e)
			}
			results <- n
		}()
	}
	wg.Wait()
	close(results)
	total := 0
	for n := range results {
		total += n
	}
	if total != 1 {
		t.Fatal(total)
	}
	var n int
	db.QueryRow(`SELECT COUNT(*) FROM alert_events`).Scan(&n)
	if n != 1 {
		t.Fatal(n)
	}
}
