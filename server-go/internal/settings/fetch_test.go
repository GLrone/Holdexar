package settings

import (
	"context"
	"database/sql"
	"encoding/json"
	"holdexar/server-go/internal/events"
	"holdexar/server-go/internal/store"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestFetchDefaultsAndNormalizedWrites(t *testing.T) {
	_, handler := newTestEnv(t)
	get := httptest.NewRecorder()
	handler.ServeHTTP(get, httptest.NewRequest("GET", "/api/v1/settings/fetch", nil))
	if get.Code != 200 {
		t.Fatalf("fetch GET status=%d body=%s", get.Code, get.Body.String())
	}
	var defaults map[string]any
	if e := json.Unmarshal(get.Body.Bytes(), &defaults); e != nil {
		t.Fatal(e)
	}
	if defaults["catalog_refresh"] != false || defaults["price_interval_hours"] != float64(6) || defaults["queue_pool"] != true {
		t.Fatalf("defaults=%v", defaults)
	}
	put := httptest.NewRecorder()
	handler.ServeHTTP(put, httptest.NewRequest("PUT", "/api/v1/settings/fetch", strings.NewReader(`{"boards":false,"queue_pool":false,"price_interval_hours":100,"hb_schedule":{"time":"2:30","days":35}}`)))
	if put.Code != 200 {
		t.Fatalf("fetch PUT status=%d body=%s", put.Code, put.Body.String())
	}
	var updated map[string]any
	if e := json.Unmarshal(put.Body.Bytes(), &updated); e != nil {
		t.Fatal(e)
	}
	if updated["boards"] != false || updated["queue_pool"] != false || updated["price_interval_hours"] != float64(72) {
		t.Fatalf("updated=%v", updated)
	}
	schedule := updated["hb_schedule"].(map[string]any)
	if schedule["time"] != "02:30" || schedule["days"] != float64(30) {
		t.Fatalf("schedule=%v", schedule)
	}
}

func fetchOptionsEnv(t *testing.T, options Options) (*sql.DB, http.Handler) {
	t.Helper()
	db, _ := newTestEnv(t)
	var path string
	if e := db.QueryRow("SELECT file FROM pragma_database_list WHERE name='main'").Scan(&path); e != nil {
		t.Fatal(e)
	}
	mux := http.NewServeMux()
	Register(mux, db, store.NewWriteGate(db, path), options)
	return db, mux
}

func TestFetchCommitBroadcastAndOnlyOffToOnKick(t *testing.T) {
	bus := events.NewBus()
	sub := bus.Subscribe()
	defer bus.Unsubscribe(sub)
	calls := [][]string{}
	db, handler := fetchOptionsEnv(t, Options{Bus: bus, Changed: func(ctx context.Context, keys, kicks []string) { calls = append(calls, append([]string{}, kicks...)) }})
	for i, body := range []string{`{"boards":false}`, `{"boards":true,"price_interval_hours":1}`, `{"boards":true}`} {
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, httptest.NewRequest("PUT", "/api/v1/settings/fetch", strings.NewReader(body)))
		if recorder.Code != 200 {
			t.Fatal(recorder.Body.String())
		}
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		event, e := sub.Get(ctx)
		if e != nil || event.Type != "settings.changed" || len(event.Data) != 1 || event.Data[0].Key != "keys" {
			t.Fatalf("event=%v err=%v", event, e)
		}
		var saved string
		if e := db.QueryRow("SELECT value_json FROM app_settings WHERE key='fetch.boards'").Scan(&saved); e != nil {
			t.Fatal(e)
		}
		if i == 0 && saved != "false" || i > 0 && saved != "true" {
			t.Fatalf("uncommitted value seen=%s", saved)
		}
	}
	if len(calls) != 3 || len(calls[0]) != 0 || len(calls[1]) != 1 || calls[1][0] != "board_topsellers" || len(calls[2]) != 0 {
		t.Fatalf("kicks=%v", calls)
	}
}

func TestFetchDatabaseFailureHasNoPartialWritesOrBroadcast(t *testing.T) {
	bus := events.NewBus()
	sub := bus.Subscribe()
	defer bus.Unsubscribe(sub)
	called := false
	db, handler := fetchOptionsEnv(t, Options{Bus: bus, Changed: func(context.Context, []string, []string) { called = true }})
	if _, e := db.Exec(`CREATE TRIGGER fail_setting BEFORE INSERT ON app_settings WHEN NEW.key='crawl.queue_pool' BEGIN SELECT RAISE(ABORT,'controlled failure'); END`); e != nil {
		t.Fatal(e)
	}
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, httptest.NewRequest("PUT", "/api/v1/settings/fetch", strings.NewReader(`{"boards":false,"queue_pool":false}`)))
	if recorder.Code != 500 {
		t.Fatalf("status=%d", recorder.Code)
	}
	var count int
	_ = db.QueryRow("SELECT count(*) FROM app_settings WHERE key IN('fetch.boards','crawl.queue_pool')").Scan(&count)
	if count != 0 || called {
		t.Fatalf("partial rows=%d callback=%t", count, called)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if event, e := sub.Get(ctx); e == nil {
		t.Fatalf("failure broadcast=%v", event)
	}
}

func TestFetchInvalidScheduleDoesNotPartiallySave(t *testing.T) {
	db, handler := newTestEnv(t)
	put := httptest.NewRecorder()
	handler.ServeHTTP(put, httptest.NewRequest("PUT", "/api/v1/settings/fetch", strings.NewReader(`{"boards":false,"hb_schedule":{"time":"25:00"}}`)))
	if put.Code != 422 {
		t.Fatalf("status=%d body=%s", put.Code, put.Body.String())
	}
	var count int
	if e := db.QueryRow("SELECT count(*) FROM app_settings WHERE key='fetch.boards'").Scan(&count); e != nil {
		t.Fatal(e)
	}
	if count != 0 {
		t.Fatal("invalid request partially saved")
	}
}

func TestFetchIntegerInputMatchesRequestAndStoredValueSemantics(t *testing.T) {
	for _, value := range []any{"1e2", "2/1", "0x10", "6.", "6__0", json.Number("1e100"), json.Number("1.1")} {
		if _, e := clippedInteger(value, 1, 72, true); e == nil {
			t.Errorf("invalid request integer accepted: %v", value)
		}
	}
	for _, value := range []any{"1_000.0", "6.00", "+6", " 6 ", json.Number("6.0000000000000001"), true} {
		if _, e := clippedInteger(value, 1, 72, true); e != nil {
			t.Errorf("valid request integer rejected: %v %v", value, e)
		}
	}
	value, ok := fetchInteger(json.Number("6.9"), false)
	if !ok || value.Int64() != 6 {
		t.Fatalf("stored int conversion=%v %v", value, ok)
	}
	if _, ok := fetchInteger("6.0", false); ok {
		t.Fatal("stored invalid integer string should fall back")
	}
}
