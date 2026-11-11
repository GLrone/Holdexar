package notifications

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"holdexar/server-go/internal/events"
	"holdexar/server-go/internal/store"
)

func TestPrefsWritesValidateAndRetainOtherTypes(t *testing.T) {
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, e := store.Open(path)
	if e != nil {
		t.Fatal(e)
	}
	defer db.Close()
	if e = store.InitializeFreshDatabase(context.Background(), db); e != nil {
		t.Fatal(e)
	}
	mux := http.NewServeMux()
	Register(mux, db, Options{Gate: store.NewWriteGate(db, path), Bus: events.NewBus()})
	request := func(body string) int {
		w := httptest.NewRecorder()
		r := httptest.NewRequest("PUT", "/api/v1/notifications/prefs", strings.NewReader(body))
		mux.ServeHTTP(w, r)
		if w.Code == 200 {
			var value map[string]any
			if e := json.Unmarshal(w.Body.Bytes(), &value); e != nil {
				t.Fatal(e)
			}
			if value["ok"] != true {
				t.Fatal(value)
			}
		}
		return w.Code
	}
	if n := request(`{"enabled":true,"types":{"RULE_PRICE":{"frequency":"daily"}},"manualRegion":"us"}`); n != 200 {
		t.Fatal(n)
	}
	if n := request(`{"quietStart":"25:00"}`); n != 400 {
		t.Fatal(n)
	}
	if n := request(`{"digestWeekday":1.5}`); n != 400 {
		t.Fatal(n)
	}
	if n := request(`{"types":{"RULE_PRICE":{"frequency":"off"}}}`); n != 200 {
		t.Fatal(n)
	}
	p, e := LoadPrefs(context.Background(), db)
	if e != nil {
		t.Fatal(e)
	}
	if p["manualRegion"] != "US" || TypeEnabled(p, "RULE_PRICE") || !TypeEnabled(p, "RULE_PCT") {
		t.Fatal(p)
	}
}
func TestQuietAndDigestBoundaries(t *testing.T) {
	now := time.Date(2026, 11, 6, 23, 30, 0, 0, time.UTC)
	p := map[string]any{"quietEnabled": true, "quietStart": "23:00", "quietEnd": "08:00", "digestTime": "08:00", "digestWeekday": json.Number("4")}
	if !InQuietWindow(now, p) || InQuietWindow(now.Add(9*time.Hour), p) {
		t.Fatal("quiet boundary")
	}
	if got := DeliveryDue(now, "weekly", p); got.Day() != 13 || got.Hour() != 8 {
		t.Fatal(got)
	}
}
