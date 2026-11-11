package notifications

import (
	"context"
	"database/sql"
	"fmt"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
)

func deliveryFixture(t *testing.T, send func(context.Context, SMTPConfig, string, string) (bool, string)) (*DeliveryService, *sql.DB) {
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
	now := func() time.Time { return time.Date(2026, 11, 6, 12, 0, 0, 0, time.UTC) }
	s := NewDelivery(db, store.NewWriteGate(db, path), DeliveryOptions{Now: now, DataDir: filepath.Dir(path), Send: send, Watched: func(context.Context) ([]int64, error) { return []int64{620}, nil }})
	_, e = db.Exec(`INSERT INTO app_settings(key,value_json) VALUES('notifications.prefs','{"enabled":true}'),('smtp.host','"capture"'),('smtp.user','"sender@example.test"'),('smtp.password','"capture-only"'),('smtp.to_addr','"capture@example.test"');
 INSERT INTO alert_events(id,alert_id,appid,region,price,triggered_at,notified,details_json) VALUES(1,1,620,'CN',3500,'2026-11-06 11:00:00.000000',0,'{"eventType":"RULE_PRICE","price":3500,"currency":"CNY","gameName":"Portal 2"}');
 INSERT INTO notification_candidates(source_kind,event_id,cycle_id,appid,region_code,event_type,category,recipient,status,attempts,created_at,updated_at,payload_json) VALUES('alert_event',1,0,620,'CN','RULE_PRICE','rules','capture@example.test','pending',0,'2026-11-06 11:00:00.000000','2026-11-06 11:00:00.000000','{"eventType":"RULE_PRICE","price":3500,"currency":"CNY","gameName":"Portal 2"}')`)
	if e != nil {
		t.Fatal(e)
	}
	return s, db
}
func TestDispatchClaimsOnceAcrossConcurrentServices(t *testing.T) {
	var sends atomic.Int32
	send := func(context.Context, SMTPConfig, string, string) (bool, string) { sends.Add(1); return true, "" }
	s, db := deliveryFixture(t, send)
	logs := make(chan string, 5)
	s.options.Log = func(format string, args ...any) { logs <- fmt.Sprintf(format, args...) }
	other := NewDelivery(db, s.gate, s.options)
	var wg sync.WaitGroup
	for _, service := range []*DeliveryService{s, other, s} {
		wg.Add(1)
		go func(s *DeliveryService) {
			defer wg.Done()
			if _, e := s.Dispatch(context.Background(), nil); e != nil {
				t.Error(e)
			}
		}(service)
	}
	wg.Wait()
	close(logs)
	correlated := false
	for line := range logs {
		if strings.Contains(line, "提醒记录=1..1") {
			correlated = true
		}
	}
	if !correlated {
		t.Error("delivery log cannot locate notification record")
	}
	var status string
	var attempts int
	db.QueryRow(`SELECT status,attempts FROM notification_candidates`).Scan(&status, &attempts)
	if sends.Load() != 1 || status != "delivered" || attempts != 1 {
		t.Fatalf("sends=%d %s/%d", sends.Load(), status, attempts)
	}
}
func TestDeliveryUnknownNeverRetriesAndTemporaryFailureHasBudget(t *testing.T) {
	for _, unknown := range []bool{false, true} {
		t.Run(map[bool]string{false: "retry", true: "unknown"}[unknown], func(t *testing.T) {
			var sends int
			s, db := deliveryFixture(t, func(context.Context, SMTPConfig, string, string) (bool, string) {
				sends++
				if unknown {
					return false, "SMTPDeliveryUnknown: reply lost"
				}
				return false, "SMTPRejected: 451 temporary"
			})
			for i := 0; i < 5; i++ {
				if _, e := s.Dispatch(context.Background(), nil); e != nil {
					t.Fatal(e)
				}
			}
			var status string
			var attempts int
			db.QueryRow(`SELECT status,attempts FROM notification_candidates`).Scan(&status, &attempts)
			expected := 3
			if unknown {
				expected = 1
			}
			if sends != expected || attempts != expected {
				t.Fatalf("%d %d", sends, attempts)
			}
			if unknown && status != "unknown" {
				t.Fatal(status)
			}
		})
	}
}
func TestPreparationFailureRestoresClaimWithoutConsumingAttempt(t *testing.T) {
	s, db := deliveryFixture(t, func(context.Context, SMTPConfig, string, string) (bool, string) {
		t.Fatal("sent after preparation failure")
		return true, ""
	})
	s.options.Render = func(context.Context, []DeliveryCandidate, *int64, map[string]any) (string, string, error) {
		panic("broken renderer")
	}
	if _, e := s.Dispatch(context.Background(), nil); e == nil {
		t.Fatal("preparation failure hidden")
	}
	var status string
	var attempts int
	db.QueryRow(`SELECT status,attempts FROM notification_candidates`).Scan(&status, &attempts)
	if status != "pending" || attempts != 0 {
		t.Fatalf("%s %d", status, attempts)
	}
}
func TestInvalidFactIsQuarantinedAndStuckSendingIsUnknown(t *testing.T) {
	s, db := deliveryFixture(t, func(context.Context, SMTPConfig, string, string) (bool, string) {
		t.Fatal("invalid fact sent")
		return true, ""
	})
	if _, e := db.Exec(`UPDATE notification_candidates SET appid=999`); e != nil {
		t.Fatal(e)
	}
	if _, e := s.Dispatch(context.Background(), nil); e != nil {
		t.Fatal(e)
	}
	var status string
	db.QueryRow(`SELECT status FROM notification_candidates`).Scan(&status)
	if status != "unknown" {
		t.Fatal(status)
	}
	if _, e := db.Exec(`UPDATE notification_candidates SET appid=620,status='sending',updated_at='2026-11-05 11:00:00.000000'`); e != nil {
		t.Fatal(e)
	}
	if _, e := s.Recover(context.Background()); e != nil {
		t.Fatal(e)
	}
	db.QueryRow(`SELECT status FROM notification_candidates`).Scan(&status)
	if status != "unknown" {
		t.Fatal(status)
	}
}
