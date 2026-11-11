package humblechoice

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"html"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"holdexar/server-go/internal/crawl"
	"holdexar/server-go/internal/notifications"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/store"
	"holdexar/server-go/internal/storebrowse"
)

func humbleDB(t *testing.T) (*sql.DB, *store.WriteGate) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err := store.InitializeFreshDatabase(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	return db, store.NewWriteGate(db, path)
}

func TestMonthlyRetryPreservesTagsThenBackfillsAndNotifiesOnce(t *testing.T) {
	db, gate := humbleDB(t)
	ctx := t.Context()
	now := time.Date(2026, 11, 8, 10, 0, 0, 0, time.UTC)
	if _, err := db.Exec(`UPDATE crawl_regions SET enabled=(code='cn');
		INSERT INTO app_settings(key,value_json) VALUES('notifications.prefs','{"enabled":true}'),
		('smtp.host','"capture"'),('smtp.user','"sender@example.test"'),('smtp.password','"capture-only"'),
		('smtp.to_addr','"capture@example.test"')`); err != nil {
		t.Fatal(err)
	}
	var complete atomic.Bool
	var searches atomic.Int32
	membership := html.EscapeString(`{"activeContentMachineName":"november_2026_choice",
		"productHumanName":"November 2026 Humble Choice","activeContentEndDate|datetime":"2026-12-01T18:00:00",
		"first":{"recommendation_copy_dict":{"text":"{}"},"title":"First Game","delivery_methods":["steam"]},
		"second":{"recommendation_copy_dict":{},"title":"Second Game Playtest","delivery_methods":["steam"]},
		"coupon":{"recommendation_copy_dict":{},"title":"Coupon","delivery_methods":["direct"]}}`)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/membership" {
			_, _ = io.WriteString(w, membership+membership)
			return
		}
		searches.Add(1)
		switch r.URL.Query().Get("term") {
		case "First Game":
			_, _ = io.WriteString(w, `{"items":[{"name":"First Game Deluxe Edition","id":40},{"name":"First Game","id":"220"}]}`)
		case "Second Game Playtest":
			if complete.Load() {
				_, _ = io.WriteString(w, `{"items":[{"name":"Second Game","id":221}]}`)
				return
			}
			fallthrough
		default:
			_, _ = io.WriteString(w, `{"items":[]}`)
		}
	}))
	defer upstream.Close()
	price := crawl.New(db, gate, nil, crawl.Options{Now: func() time.Time { return now }, Fetch: func(_ context.Context, ids []int64, _, _ string, _ bool) (map[int64]storebrowse.Item, error) {
		items := make(map[int64]storebrowse.Item, len(ids))
		for _, id := range ids {
			name := fmt.Sprintf("Game %d", id)
			items[id] = storebrowse.Item{"appid": id, "success": 1, "visible": true, "type": 0, "name": name,
				"purchase_options": []any{map[string]any{"packageid": "39", "purchase_option_name": name,
					"final_price_in_cents": "999", "original_price_in_cents": "1999"}}}
		}
		return items, nil
	}})
	s := New(db, gate, Options{Client: upstream.Client(), MembershipURL: upstream.URL + "/membership", SearchURL: upstream.URL + "/search",
		Now: func() time.Time { return now }, Crawl: price.RunHumbleBackfill})
	mux := http.NewServeMux()
	s.Register(mux)
	call := func(method, route string) map[string]any {
		t.Helper()
		w := httptest.NewRecorder()
		mux.ServeHTTP(w, httptest.NewRequest(method, "/api/v1/metadata/hb/"+route, nil))
		var data map[string]any
		if w.Code != 200 || json.Unmarshal(w.Body.Bytes(), &data) != nil {
			t.Fatalf("HTTP %d: %s", w.Code, w.Body)
		}
		return data
	}
	first, err := s.Run(ctx, false)
	if err != nil || first["recorded"] != false || len(first["marked"].([]mark)) != 1 {
		t.Fatalf("incomplete month: %+v %v", first, err)
	}
	hbCount(t, db, `SELECT count(*) FROM app_settings WHERE key='hb_choice_last_month'`, 0)
	hbCount(t, db, `SELECT count(*) FROM fact_notices WHERE source='hb_choice'`, 0)
	hbCount(t, db, `SELECT count(*) FROM crawl_jobs WHERE kind='hb_backfill' AND json_extract(stats_json,'$.trigger')='automatic' AND status='done'`, 1)
	if _, err := db.Exec("UPDATE games SET hb_data='HB慈善包26年8月包' WHERE appid=220"); err != nil {
		t.Fatal(err)
	}
	complete.Store(true)
	finished := call("POST", "refresh")
	if finished["recorded"] != true || finished["backfill"].(map[string]any)["pending"] != float64(1) {
		t.Fatalf("completed refresh: %+v", finished)
	}
	hbCount(t, db, `SELECT count(*) FROM games WHERE appid=220 AND hb_data='HB慈善包26年8月包, HB慈善包26年11月包'`, 1)
	hbCount(t, db, `SELECT count(*) FROM notification_candidates WHERE event_type='HB_CHOICE' AND status='pending'`, 1)
	before := searches.Load()
	skipped, err := s.Run(ctx, false)
	if err != nil || skipped["skipped"] != true || searches.Load() != before {
		t.Fatalf("repeat scheduled refresh: %+v %v", skipped, err)
	}
	_ = call("POST", "refresh")
	hbCount(t, db, `SELECT count(*) FROM fact_notices WHERE source='hb_choice'`, 1)
	hbCount(t, db, `SELECT count(*) FROM crawl_jobs WHERE kind='hb_backfill'`, 2)
	offers := call("GET", "offers")
	if offers["monthUrl"] != membershipURL+"/November-2026" || len(offers["games"].([]any)) != 2 ||
		offers["games"].([]any)[0].(map[string]any)["priceFen"] != float64(999) {
		t.Fatalf("price read: %+v", offers)
	}
	var delivered int
	delivery := notifications.NewDelivery(db, gate, notifications.DeliveryOptions{Now: func() time.Time { return now.Add(time.Minute).In(sched.DefaultLoc) },
		Send: func(_ context.Context, _ notifications.SMTPConfig, subject, body string) (bool, string) {
			if !strings.Contains(subject, "Humble Choice") && !strings.Contains(body, "Humble Choice") {
				t.Errorf("wrong captured subject %q", subject)
			}
			delivered++
			return true, ""
		}})
	if _, err := delivery.Dispatch(ctx, nil); err != nil || delivered != 1 {
		t.Fatalf("captured %d messages, error %v", delivered, err)
	}
}

func TestHumbleFactFailureRollsBackMonthAndLegacyFactPreventsDuplicate(t *testing.T) {
	db, gate := humbleDB(t)
	ctx := t.Context()
	s := New(db, gate, Options{})
	m := month{Machine: "november_2026_choice", Product: "November 2026 Humble Choice"}
	label := monthLabel(m)
	marked := []mark{{220, "Fixture title"}}
	if _, err := db.Exec(`INSERT INTO app_settings(key,value_json) VALUES('notifications.prefs','{"enabled":true}'),
		('smtp.to_addr','"capture@example.test"');
		CREATE TRIGGER reject_humble_notice BEFORE INSERT ON notification_candidates BEGIN SELECT RAISE(ABORT,'fixture rejected notice'); END`); err != nil {
		t.Fatal(err)
	}
	if err := s.persist(ctx, m, label, marked, true); err == nil {
		t.Fatal("a failed notice was ignored")
	}
	hbCount(t, db, "SELECT count(*) FROM games WHERE appid=220", 0)
	hbCount(t, db, "SELECT count(*) FROM app_settings WHERE key='hb_choice_last_month'", 0)
	hbCount(t, db, "SELECT count(*) FROM fact_notices WHERE source='hb_choice'", 0)
	if _, err := db.Exec("DROP TRIGGER reject_humble_notice"); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("INSERT INTO fact_notices(source,kind,fact_key) VALUES('hb_choice','bundle_changed',?)", "hb_choice:"+label); err != nil {
		t.Fatal(err)
	}
	if err := s.persist(ctx, m, label, marked, true); err != nil {
		t.Fatal(err)
	}
	hbCount(t, db, "SELECT count(*) FROM fact_notices WHERE source='hb_choice'", 1)
	hbCount(t, db, "SELECT count(*) FROM notification_candidates WHERE event_type='HB_CHOICE'", 0)
	hbCount(t, db, "SELECT count(*) FROM games WHERE appid=220 AND is_hb=1", 1)
}

func TestAutomaticPriceOffDoesNotSuppressManualBackfill(t *testing.T) {
	db, gate := humbleDB(t)
	ctx := t.Context()
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "outage", http.StatusServiceUnavailable)
	}))
	defer upstream.Close()
	var calls int
	s := New(db, gate, Options{Client: upstream.Client(), MembershipURL: upstream.URL,
		AutoPrice: func(context.Context) (bool, error) { return false, nil },
		Crawl: func(_ context.Context, ids []int64) (int64, error) {
			calls++
			if len(ids) != 1 || ids[0] != 220 {
				t.Fatalf("wrong backfill list %v", ids)
			}
			return 1, nil
		}})
	if err := s.persist(ctx, month{}, "HB慈善包26年8月包", []mark{{220, "First"}, {221, "Second"}, {222, "Third"}, {223, "Fourth"}}, false); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`UPDATE games SET free_kind='f2p' WHERE appid=221;
		UPDATE games SET removed_at='2026-11-01' WHERE appid=222;
		INSERT INTO game_current_prices(appid,region_code,discount_percent,price_status,fail_count) VALUES(223,'CN',0,'missing',1)`); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Run(ctx, false); err != nil || calls != 0 {
		t.Fatalf("scheduled while disabled: calls=%d err=%v", calls, err)
	}
	if result, err := s.Run(ctx, true); err != nil || result["ok"] != false || calls != 1 {
		t.Fatalf("manual during outage: calls=%d result=%+v err=%v", calls, result, err)
	}
}

func hbCount(t *testing.T, db *sql.DB, query string, want int) {
	t.Helper()
	var n int
	if err := db.QueryRow(query).Scan(&n); err != nil || n != want {
		t.Fatalf("%s returned %d, want %d, err %v", query, n, want, err)
	}
}

func TestResolveBundledDLCRequiresExactBaseTitle(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("term") != "Mafia The Old Country" {
			t.Errorf("unexpected search query %q", r.URL.Query().Get("term"))
		}
		_, _ = io.WriteString(w, `{"items":[{"name":"Mafia: The Old Country - Padrino Pack","id":3269260},{"name":"Mafia: The Old Country","id":1941540}]}`)
	}))
	defer upstream.Close()
	s := New(nil, nil, Options{Client: upstream.Client(), SearchURL: upstream.URL})
	if id := s.resolve(t.Context(), "Mafia: The Old Country + 2 DLC Packs"); id != 1941540 {
		t.Fatalf("got app id %d", id)
	}
	if id := pickAppID("Mafia: The Old Country", []searchHit{{ID: json.RawMessage("3269260"), Name: "Mafia: The Old Country Deluxe Edition"}}, true); id != 0 {
		t.Fatalf("DLC-only result admitted: %d", id)
	}
}
