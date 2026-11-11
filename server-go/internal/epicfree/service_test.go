package epicfree

import (
	"context"
	"database/sql"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"holdexar/server-go/internal/notifications"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/store"
)

func epicServiceDB(t *testing.T) (*sql.DB, *store.WriteGate) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err := store.InitializeFreshDatabase(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	return db, store.NewWriteGate(db, path)
}

func TestOffersAndRefreshSharePromotionHistoryAndNotificationIdentity(t *testing.T) {
	ctx := context.Background()
	db, gate := epicServiceDB(t)
	if _, err := db.Exec(`INSERT INTO app_settings(key,value_json) VALUES('notifications.prefs','{"enabled":true}'),
		('smtp.host','"capture"'),('smtp.user','"sender@example.test"'),('smtp.password','"capture-only"'),
		('smtp.to_addr','"capture@example.test"')`); err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 11, 8, 11, 0, 0, 0, time.UTC)
	var polls, phase atomic.Int32
	noMobile := func(context.Context) (*MobileOffer, error) { return nil, nil }
	opts := Options{
		Now: func() time.Time { return now },
		Fetch: func(context.Context) ([]Offer, error) {
			polls.Add(1)
			return []Offer{
				{Title: "First Game", Start: "2026-11-5", End: "2026-11-12"},
				{Title: "Second Game", TitleCn: "第二款", Start: "2026-11-8", End: "2026-11-18", Upcoming: phase.Load() == 0},
			}, nil
		},
		Match: func(_ context.Context, offers []Offer) []Offer {
			for i := range offers {
				id := int64(220 + i)
				offers[i].AppID = &id
			}
			return offers
		},
		Mobile: noMobile, Breaker: noMobile,
		MobileFromPC: func(context.Context, []Offer) (*MobileOffer, error) { return nil, nil },
	}
	s := New(ctx, db, gate, opts)
	defer s.Close(ctx)
	initial, err := s.Offers(ctx, false)
	if err != nil || !initial.OK || len(initial.Offers) != 2 {
		t.Fatalf("initial view: %+v %v", initial, err)
	}
	assertEpicCount(t, db, "SELECT count(*) FROM fact_notices WHERE source='epic_free'", 0)
	if result, err := s.Refresh(ctx); err != nil || result["ok"] != true {
		t.Fatalf("initial marking: %v %v", result, err)
	}
	assertEpicCount(t, db, "SELECT count(*) FROM games WHERE is_epic=1 AND updated_at IS NULL", 2)
	assertEpicCount(t, db, "SELECT count(*) FROM fact_notices WHERE source='epic_free'", 1)
	var captured []string
	delivery := notifications.NewDelivery(db, gate, notifications.DeliveryOptions{
		Now: func() time.Time { return now.Add(time.Minute).In(sched.DefaultLoc) },
		Send: func(_ context.Context, smtp notifications.SMTPConfig, subject, body string) (bool, string) {
			if smtp.ToAddr != "capture@example.test" {
				t.Errorf("wrong fixture recipient: %s", smtp.ToAddr)
			}
			captured = append(captured, subject+" "+body)
			return true, ""
		},
	})
	if _, err := delivery.Dispatch(ctx, nil); err != nil {
		t.Fatal(err)
	}
	phase.Store(1)
	if _, err := s.Offers(ctx, true); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Refresh(ctx); err != nil {
		t.Fatal(err)
	}
	assertEpicCount(t, db, "SELECT count(*) FROM fact_notices WHERE source='epic_free'", 2)
	assertEpicCount(t, db, "SELECT count(*) FROM notification_candidates WHERE event_type='EPIC_FREE' AND recipient='capture@example.test'", 2)
	for range 2 {
		if _, err := delivery.Dispatch(ctx, nil); err != nil {
			t.Fatal(err)
		}
	}
	if len(captured) != 2 || !strings.Contains(captured[0], "First Game") || !strings.Contains(captured[1], "第二款") {
		t.Fatalf("Epic reminders were omitted or duplicated: %d", len(captured))
	}
	assertEpicCount(t, db, "SELECT count(*) FROM games WHERE appid=221 AND epic_date='2026-11-8'", 1)
	if _, err := db.Exec("INSERT INTO app_settings(key,value_json) VALUES('fetch.epic_free','false') ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json"); err != nil {
		t.Fatal(err)
	}
	before := polls.Load()
	restarted := New(ctx, db, gate, opts)
	defer restarted.Close(ctx)
	loaded, err := restarted.Offers(ctx, false)
	if err != nil || !loaded.Cached || !loaded.Stale || len(loaded.Offers) != 2 || polls.Load() != before {
		t.Fatalf("disabled restart: %+v %v; polls=%d", loaded, err, polls.Load())
	}
}

func TestLegacyNoticeAndFailedCandidateCanRetryWithoutDuplicate(t *testing.T) {
	db, gate := epicServiceDB(t)
	ctx := context.Background()
	s := New(ctx, db, gate, Options{})
	defer s.Close(ctx)
	if _, err := db.Exec(`INSERT INTO app_settings(key,value_json) VALUES('notifications.prefs','{"enabled":true}'),
		('smtp.to_addr','"capture@example.test"');
		INSERT INTO fact_notices(source,kind,fact_key) VALUES('epic_free','free_rotation','epic_free:title:older|title:known');
		CREATE TRIGGER epic_candidate_fixture BEFORE INSERT ON notification_candidates
		BEGIN SELECT RAISE(ABORT,'fixture rejected candidate'); END`); err != nil {
		t.Fatal(err)
	}
	offers := []Offer{{Title: "Known"}, {Title: "New"}}
	if _, err := s.recordNotices(ctx, offers, time.Now().In(sched.DefaultLoc)); err == nil {
		t.Fatal("candidate failure was swallowed")
	}
	assertEpicCount(t, db, "SELECT count(*) FROM fact_notices WHERE source='epic_free'", 1)
	if _, err := db.Exec("DROP TRIGGER epic_candidate_fixture"); err != nil {
		t.Fatal(err)
	}
	if n, err := s.recordNotices(ctx, offers, time.Now().In(sched.DefaultLoc)); err != nil || n != 1 {
		t.Fatalf("retry: %d %v", n, err)
	}
	assertEpicCount(t, db, "SELECT count(*) FROM fact_notices WHERE source='epic_free'", 2)
	assertEpicCount(t, db, "SELECT count(*) FROM notification_candidates WHERE event_type='EPIC_FREE'", 1)
}

func TestImportHTTPRetainsFirstGiftAndSteamOffersReadPriceState(t *testing.T) {
	db, gate := epicServiceDB(t)
	ctx := context.Background()
	now := time.Date(2026, 11, 8, 5, 0, 0, 0, time.UTC)
	s := New(ctx, db, gate, Options{Now: func() time.Time { return now }})
	defer s.Close(ctx)
	mux := http.NewServeMux()
	s.Register(mux)
	post := func(body string) *httptest.ResponseRecorder {
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/api/v1/metadata/epic/list", strings.NewReader(body)))
		return rec
	}
	if w := post(`{"games":[{"appid":"220","title":"Saved name","free_start":"2026年5月08日"},{"appid":221,"free_start":"2026-02-31"}]}`); w.Code != 200 || !strings.Contains(w.Body.String(), `"freeStart":"2026-5-8"`) {
		t.Fatalf("historical list: %d %s", w.Code, w.Body)
	}
	if w := post(`{"games":[{"appid":220,"title":"New name","free_start":"2026/12/1"}]}`); w.Code != 200 || !strings.Contains(w.Body.String(), `"marked":[]`) {
		t.Fatalf("repeat history import: %d %s", w.Code, w.Body)
	}
	if w := post(`{"games":[{"appid":900},{"appid":"bad"}]}`); w.Code != 422 {
		t.Fatalf("invalid list accepted: %d %s", w.Code, w.Body)
	}
	assertEpicCount(t, db, "SELECT count(*) FROM games WHERE appid=220 AND name='Saved name' AND epic_date='2026-5-8'", 1)
	assertEpicCount(t, db, "SELECT count(*) FROM games WHERE appid=221 AND epic_date IS NULL AND is_epic=1", 1)
	assertEpicCount(t, db, "SELECT count(*) FROM games WHERE appid=900", 0)
	if _, err := db.Exec("UPDATE games SET free_kind='promo',promo_end_at=? WHERE appid=220", now.Unix()+3600); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("UPDATE games SET free_kind='promo',promo_end_at=? WHERE appid=221", now.Unix()-1); err != nil {
		t.Fatal(err)
	}
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/v1/metadata/steam/offers", nil))
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"appid":220`) || strings.Contains(w.Body.String(), `"appid":221`) {
		t.Fatalf("Steam offer query: %d %s", w.Code, w.Body)
	}
}

func assertEpicCount(t *testing.T, db *sql.DB, query string, want int) {
	t.Helper()
	var got int
	if err := db.QueryRow(query).Scan(&got); err != nil || got != want {
		t.Fatalf("%s: got=%d want=%d err=%v", query, got, want, err)
	}
}
