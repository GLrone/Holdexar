package notifications

import (
	"context"
	"database/sql"
	"encoding/json"
	"holdexar/server-go/internal/store"
	"strings"
	"testing"
	"time"
)

func seedPriceEvents(t *testing.T, db *sql.DB, count int) {
	t.Helper()
	_, e := db.Exec(`INSERT INTO price_cycles(id,kind,status,scope,batches_done,notification_scan_json) VALUES(1,'scheduled','completed','pool',1,'{"state":"pending","cursor":0}');
 WITH RECURSIVE seq(n) AS(SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n<?) INSERT INTO price_events(id,cycle_id,appid,region_code,event_type,occurred_at,created_at,previous_json,current_json) SELECT n,1,n+619,'CN','PRICE_DROP','2026-11-06 11:00:00.000000','2026-11-06 11:00:00.000000','{"price":5000}','{"price":3500}' FROM seq`, count)
	if e != nil {
		t.Fatal(e)
	}
}
func TestPriceScanResumesCommittedPageAndDoesNotDuplicate(t *testing.T) {
	s, db := deliveryFixture(t, func(context.Context, SMTPConfig, string, string) (bool, string) { return true, "" })
	seedPriceEvents(t, db, 1001)
	ctx := context.Background()
	prefs, e := LoadPrefs(ctx, db)
	if e != nil {
		t.Fatal(e)
	}
	scope := make([]int64, 1001)
	for i := range scope {
		scope[i] = int64(620 + i)
	}
	scan, owned, e := s.openEventScan(ctx, 1, prefs, scope, "capture@example.test")
	if e != nil || !owned || scan == nil {
		t.Fatalf("%v %v", scan, e)
	}
	page, e := s.scanPage(ctx, 1, *scan)
	if e != nil || len(page) != 1000 {
		t.Fatalf("%d %v", len(page), e)
	}
	if n, e := s.commitEventPage(ctx, 1, *scan, true, page); e != nil || n != 1000 {
		t.Fatalf("%d %v", n, e)
	}
	restarted := NewDelivery(db, s.gate, s.options)
	if n, e := restarted.ResumeEventScans(ctx, nil); e != nil || n != 1 {
		t.Fatalf("resume %d %v", n, e)
	}
	if n, e := restarted.ResumeEventScans(ctx, nil); e != nil || n != 0 {
		t.Fatalf("replay %d %v", n, e)
	}
	var raw string
	db.QueryRow(`SELECT notification_scan_json FROM price_cycles WHERE id=1`).Scan(&raw)
	var state eventScan
	if e = json.Unmarshal([]byte(raw), &state); e != nil || state.State != "done" || state.Cursor != 1001 {
		t.Fatalf("%s %v", raw, e)
	}
}
func TestClearedRecipientKeepsPriceScanPageUnconsumed(t *testing.T) {
	s, db := deliveryFixture(t, nil)
	seedPriceEvents(t, db, 1)
	ctx := context.Background()
	prefs, e := LoadPrefs(ctx, db)
	if e != nil {
		t.Fatal(e)
	}
	scan, _, e := s.openEventScan(ctx, 1, prefs, []int64{620}, "capture@example.test")
	if e != nil {
		t.Fatal(e)
	}
	if _, e = db.Exec(`UPDATE app_settings SET value_json='""' WHERE key='smtp.to_addr'`); e != nil {
		t.Fatal(e)
	}
	page, e := s.scanPage(ctx, 1, *scan)
	if e != nil {
		t.Fatal(e)
	}
	if _, e = s.commitEventPage(ctx, 1, *scan, true, page); e == nil {
		t.Fatal("cleared recipient consumed page")
	}
	current, _, e := readEventScan(ctx, db, 1)
	if e != nil || current.Cursor != 0 {
		t.Fatalf("%v %v", current, e)
	}
}
func TestRecipientRetargetPreservesProtectedRowsAndFailureBudget(t *testing.T) {
	s, db := deliveryFixture(t, nil)
	_, e := db.Exec(`UPDATE notification_candidates SET status='failed',attempts=2,last_error='SMTPRejected: 451 temporary';
 INSERT INTO notification_candidates(source_kind,event_id,cycle_id,appid,region_code,event_type,category,recipient,status,attempts,created_at,updated_at,last_error) VALUES('alert_event',1,0,620,'CN','RULE_PRICE','rules','new@example.test','failed',1,'2026-11-06 11:00:00.000000','2026-11-06 11:00:00.000000','SMTPRejected: 451 temporary')`)
	if e != nil {
		t.Fatal(e)
	}
	cfg := SMTPConfig{Host: "capture", Port: 465, User: "sender@example.test", ToAddr: "new@example.test", UseSSL: true}
	if e = s.SaveSMTP(context.Background(), cfg); e != nil {
		t.Fatal(e)
	}
	var count, attempts int
	var recipient, history string
	db.QueryRow(`SELECT COUNT(*) FROM notification_candidates`).Scan(&count)
	db.QueryRow(`SELECT recipient,attempts,delivery_history_json FROM notification_candidates`).Scan(&recipient, &attempts, &history)
	if count != 1 || recipient != "new@example.test" || attempts != 3 || !strings.Contains(history, "capture@example.test") {
		t.Fatalf("%d %s %d %s", count, recipient, attempts, history)
	}
	if _, e = db.Exec(`UPDATE notification_candidates SET status='unknown'`); e != nil {
		t.Fatal(e)
	}
	cfg.ToAddr = "third@example.test"
	if e = s.SaveSMTP(context.Background(), cfg); e != nil {
		t.Fatal(e)
	}
	db.QueryRow(`SELECT recipient FROM notification_candidates`).Scan(&recipient)
	if recipient != "new@example.test" {
		t.Fatal("retargeted protected result")
	}
}
func TestDueAtUsesLocalWallClockAndRulePauseIsRespected(t *testing.T) {
	s, db := deliveryFixture(t, func(context.Context, SMTPConfig, string, string) (bool, string) { return true, "" })
	zone := time.FixedZone("test", 8*3600)
	s.options.Now = func() time.Time { return time.Date(2026, 11, 6, 12, 0, 0, 0, zone) }
	if _, e := db.Exec(`UPDATE notification_candidates SET due_at='2026-11-06 12:00:00.000000'`); e != nil {
		t.Fatal(e)
	}
	result, e := s.Dispatch(context.Background(), nil)
	if e != nil || result.Sent != 1 {
		t.Fatalf("due at wall clock %v %v", result, e)
	}
}

func TestFailedPricePageRollsBackCandidatesAndCursor(t *testing.T) {
	s, db := deliveryFixture(t, nil)
	seedPriceEvents(t, db, 1)
	ctx := context.Background()
	prefs, e := LoadPrefs(ctx, db)
	if e != nil {
		t.Fatal(e)
	}
	scan, _, e := s.openEventScan(ctx, 1, prefs, []int64{620}, "capture@example.test")
	if e != nil {
		t.Fatal(e)
	}
	page, e := s.scanPage(ctx, 1, *scan)
	if e != nil {
		t.Fatal(e)
	}
	if _, e = db.Exec(`CREATE TRIGGER reject_price_candidate BEFORE INSERT ON notification_candidates WHEN NEW.source_kind='price_event' BEGIN SELECT RAISE(ABORT,'test rejection'); END`); e != nil {
		t.Fatal(e)
	}
	if _, e = s.commitEventPage(ctx, 1, *scan, true, page); e == nil {
		t.Fatal("failed page committed")
	}
	saved, _, e := readEventScan(ctx, db, 1)
	if e != nil || saved.Cursor != 0 {
		t.Fatalf("%v %v", saved, e)
	}
	var count int
	db.QueryRow(`SELECT COUNT(*) FROM notification_candidates WHERE source_kind='price_event'`).Scan(&count)
	if count != 0 {
		t.Fatal(count)
	}
}

func TestNewPriceFactsAfterFrozenUpperRemainPending(t *testing.T) {
	s, db := deliveryFixture(t, nil)
	seedPriceEvents(t, db, 1)
	ctx := context.Background()
	s.options.Watched = func(context.Context) ([]int64, error) { return []int64{620, 621}, nil }
	prefs, e := LoadPrefs(ctx, db)
	if e != nil {
		t.Fatal(e)
	}
	scan, _, e := s.openEventScan(ctx, 1, prefs, []int64{620, 621}, "capture@example.test")
	if e != nil {
		t.Fatal(e)
	}
	page, e := s.scanPage(ctx, 1, *scan)
	if e != nil {
		t.Fatal(e)
	}
	if _, e = s.commitEventPage(ctx, 1, *scan, true, page); e != nil {
		t.Fatal(e)
	}
	e = s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		_, e := conn.ExecContext(ctx, `INSERT INTO price_events(id,cycle_id,appid,region_code,event_type,occurred_at,created_at) VALUES(2,1,621,'CN','PRICE_DROP','2026-11-06 11:00:00.000000','2026-11-06 11:00:00.000000')`)
		return e
	})
	if e != nil {
		t.Fatal(e)
	}
	if e = s.completeEventScan(ctx, 1, *scan); e != nil {
		t.Fatal(e)
	}
	saved, _, e := readEventScan(ctx, db, 1)
	if e != nil || saved.State != "pending" || saved.Cursor != 1 {
		t.Fatalf("%v %v", saved, e)
	}
	if n, e := s.ResumeEventScans(ctx, nil); e != nil || n != 1 {
		t.Fatalf("%d %v", n, e)
	}
}

func TestPriceEventDailyAndWeeklyFrequencyDefersUntilDue(t *testing.T) {
	for _, frequency := range []string{"daily", "weekly"} {
		t.Run(frequency, func(t *testing.T) {
			sends := 0
			s, db := deliveryFixture(t, func(context.Context, SMTPConfig, string, string) (bool, string) { sends++; return true, "" })
			seedPriceEvents(t, db, 1)
			if _, e := db.Exec(`DELETE FROM notification_candidates;UPDATE app_settings SET value_json=? WHERE key='notifications.prefs'`, `{"enabled":true,"digestTime":"13:00","digestWeekday":0,"types":{"PRICE_DROP":{"frequency":"`+frequency+`"}}}`); e != nil {
				t.Fatal(e)
			}
			result, e := s.Dispatch(context.Background(), nil)
			if e != nil || result.Sent != 0 || sends != 0 {
				t.Fatalf("sent before due: %v %d %v", result, sends, e)
			}
			day := 6
			if frequency == "weekly" {
				day = 9
			}
			s.options.Now = func() time.Time { return time.Date(2026, 11, day, 13, 0, 0, 0, time.UTC) }
			result, e = s.Dispatch(context.Background(), nil)
			if e != nil || result.Sent != 1 || sends != 1 {
				t.Fatalf("due: %v %d %v", result, sends, e)
			}
		})
	}
}

func TestLegacyPendingPriceCandidateGetsDueWithoutResettingAttempts(t *testing.T) {
	sends := 0
	s, db := deliveryFixture(t, func(context.Context, SMTPConfig, string, string) (bool, string) { sends++; return true, "" })
	seedPriceEvents(t, db, 1)
	_, e := db.Exec(`UPDATE app_settings SET value_json='{"enabled":true,"digestTime":"13:00","types":{"PRICE_DROP":{"frequency":"daily"}}}' WHERE key='notifications.prefs';UPDATE notification_candidates SET source_kind='price_event',cycle_id=1,event_type='PRICE_DROP',category='price',payload_json=NULL,status='failed',attempts=2,last_error='SMTPRejected: 451 temporary',due_at=NULL`)
	if e != nil {
		t.Fatal(e)
	}
	result, e := s.Dispatch(context.Background(), nil)
	if e != nil || result.Sent != 0 || sends != 0 {
		t.Fatalf("%v %d %v", result, sends, e)
	}
	var attempts int
	var due string
	db.QueryRow(`SELECT attempts,due_at FROM notification_candidates`).Scan(&attempts, &due)
	dueTime, parseErr := parseFactTime(due)
	if attempts != 2 || parseErr != nil || !dueTime.Equal(time.Date(2026, 11, 6, 13, 0, 0, 0, time.UTC)) {
		t.Fatalf("%d %s", attempts, due)
	}
}
