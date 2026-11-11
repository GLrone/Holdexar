package epicfree

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func lifecycleOptions(fetch func(context.Context) ([]Offer, error)) Options {
	noMobile := func(context.Context) (*MobileOffer, error) { return nil, nil }
	return Options{Fetch: fetch, Mobile: noMobile, Breaker: noMobile,
		MobileFromPC: func(context.Context, []Offer) (*MobileOffer, error) { return nil, nil }}
}

func TestModuleDisablePersistsAndGatesEveryEpicEntryButNotSteam(t *testing.T) {
	db, gate := epicServiceDB(t)
	ctx := t.Context()
	var calls atomic.Int32
	opts := lifecycleOptions(func(context.Context) ([]Offer, error) {
		calls.Add(1)
		return []Offer{}, nil
	})
	s := New(ctx, db, gate, opts)
	t.Cleanup(func() { s.Close(context.Background()) })
	if _, err := s.Offers(ctx, true); err != nil {
		t.Fatal(err)
	}
	if err := s.SetEnabled(ctx, false); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Offers(ctx, false); !errors.Is(err, ErrDisabled) {
		t.Fatalf("cached offers bypassed disabled module: %v", err)
	}
	mux := http.NewServeMux()
	s.Register(mux)
	for _, req := range []struct{ method, path, body string }{
		{"GET", "/api/v1/metadata/epic/offers", ""},
		{"GET", "/api/v1/metadata/epic/offers?force=true", ""},
		{"POST", "/api/v1/metadata/epic/refresh", ""},
		{"POST", "/api/v1/metadata/epic/list", `{"games":[{"appid":220}]}`},
	} {
		w := httptest.NewRecorder()
		mux.ServeHTTP(w, httptest.NewRequest(req.method, req.path, strings.NewReader(req.body)))
		if w.Code != 409 {
			t.Fatalf("%s returned %d: %s", req.path, w.Code, w.Body)
		}
	}
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, httptest.NewRequest("GET", "/api/v1/metadata/steam/offers", nil))
	if w.Code != 200 {
		t.Fatalf("Steam offers affected: %d %s", w.Code, w.Body)
	}
	assertEpicCount(t, db, "SELECT count(*) FROM games WHERE appid=220", 0)
	restarted := New(ctx, db, gate, opts)
	t.Cleanup(func() { restarted.Close(context.Background()) })
	if on, err := restarted.Enabled(ctx); err != nil || on {
		t.Fatalf("disabled state lost at restart: %v %v", on, err)
	}
	for range 3 {
		if err := s.SetEnabled(ctx, true); err != nil {
			t.Fatal(err)
		}
		if _, err := s.Offers(ctx, true); err != nil {
			t.Fatal(err)
		}
		if err := s.SetEnabled(ctx, false); err != nil {
			t.Fatal(err)
		}
	}
	if calls.Load() != 4 {
		t.Fatalf("duplicate or omitted fetch: %d", calls.Load())
	}
}

func TestModuleDisableCancelsAndWaitsForInFlightCleanup(t *testing.T) {
	db, gate := epicServiceDB(t)
	entered, canceled, release := make(chan struct{}), make(chan struct{}), make(chan struct{})
	s := New(t.Context(), db, gate, lifecycleOptions(func(ctx context.Context) ([]Offer, error) {
		close(entered)
		<-ctx.Done()
		close(canceled)
		<-release
		return nil, ctx.Err()
	}))
	t.Cleanup(func() { s.Close(context.Background()) })
	offerDone := make(chan error, 1)
	go func() { _, err := s.Offers(t.Context(), true); offerDone <- err }()
	<-entered
	disabled := make(chan error, 1)
	go func() { disabled <- s.SetEnabled(t.Context(), false) }()
	select {
	case <-canceled:
	case <-time.After(3 * time.Second):
		t.Fatal("disable did not cancel fetch")
	}
	select {
	case err := <-disabled:
		t.Fatalf("disable returned before cleanup: %v", err)
	default:
	}
	close(release)
	if err := <-disabled; err != nil {
		t.Fatal(err)
	}
	if err := <-offerDone; err == nil {
		t.Fatal("canceled fetch reported success")
	}
	assertEpicCount(t, db, "SELECT count(*) FROM app_settings WHERE key='metadata.epic_offers_cache'", 0)
}

func TestManualRequestCancellationReachesFetch(t *testing.T) {
	db, gate := epicServiceDB(t)
	entered, exited := make(chan struct{}), make(chan struct{})
	s := New(t.Context(), db, gate, lifecycleOptions(func(ctx context.Context) ([]Offer, error) {
		close(entered)
		<-ctx.Done()
		close(exited)
		return nil, ctx.Err()
	}))
	t.Cleanup(func() { s.Close(context.Background()) })
	ctx, cancel := context.WithCancel(t.Context())
	done := make(chan error, 1)
	go func() { _, err := s.Offers(ctx, true); done <- err }()
	<-entered
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatalf("manual cancellation: %v", err)
	}
	select {
	case <-exited:
	case <-time.After(3 * time.Second):
		t.Fatal("request cancellation leaked fetch")
	}
}

func TestFailedModulePersistenceKeepsCurrentState(t *testing.T) {
	db, gate := epicServiceDB(t)
	s := New(t.Context(), db, gate, Options{})
	t.Cleanup(func() { s.Close(context.Background()) })
	if _, err := db.Exec(`CREATE TRIGGER reject_module_setting BEFORE INSERT ON app_settings
		WHEN NEW.key='module.epic_free.enabled' BEGIN SELECT RAISE(ABORT,'write rejected'); END`); err != nil {
		t.Fatal(err)
	}
	if err := s.SetEnabled(t.Context(), false); err == nil {
		t.Fatal("failed persistence reported success")
	}
	if enabled, err := s.Enabled(t.Context()); err != nil || !enabled {
		t.Fatalf("failed write changed effective state: %v %v", enabled, err)
	}
}

func TestDisableTimeoutRetainsDisabledStateAndWaitsBeforeReenable(t *testing.T) {
	db, gate := epicServiceDB(t)
	entered, canceled, release := make(chan struct{}), make(chan struct{}), make(chan struct{})
	s := New(t.Context(), db, gate, lifecycleOptions(func(ctx context.Context) ([]Offer, error) {
		close(entered)
		<-ctx.Done()
		close(canceled)
		<-release
		return nil, ctx.Err()
	}))
	t.Cleanup(func() { s.Close(context.Background()) })
	go s.Offers(t.Context(), true)
	<-entered
	ctx, cancel := context.WithCancel(t.Context())
	done := make(chan error, 1)
	go func() { done <- s.SetEnabled(ctx, false) }()
	<-canceled
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatalf("disable timeout: %v", err)
	}
	if on, err := s.Enabled(t.Context()); err != nil || on {
		t.Fatalf("timeout reactivated module: %v %v", on, err)
	}
	if enabled, running, err := s.State(t.Context()); err != nil || enabled || !running {
		t.Fatalf("canceled work hidden while cleanup remains: enabled=%v running=%v err=%v", enabled, running, err)
	}
	go func() { done <- s.SetEnabled(t.Context(), true) }()
	select {
	case err := <-done:
		t.Fatalf("enabled before old cleanup: %v", err)
	default:
	}
	close(release)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if err := s.SetEnabled(t.Context(), false); err != nil {
		t.Fatal(err)
	}
	if enabled, running, err := s.State(t.Context()); err != nil || enabled || running {
		t.Fatalf("cleaned module still reports running: enabled=%v running=%v err=%v", enabled, running, err)
	}
}

func TestModuleSwitchDoesNotChangeAutomaticFetchPreference(t *testing.T) {
	db, gate := epicServiceDB(t)
	if _, err := db.Exec(`INSERT INTO app_settings(key,value_json) VALUES('fetch.epic_free','false')`); err != nil {
		t.Fatal(err)
	}
	var calls atomic.Int32
	s := New(t.Context(), db, gate, lifecycleOptions(func(context.Context) ([]Offer, error) {
		calls.Add(1)
		return []Offer{}, nil
	}))
	t.Cleanup(func() { s.Close(context.Background()) })
	if _, err := s.Offers(t.Context(), false); err != nil || calls.Load() != 0 {
		t.Fatalf("automatic fetch preference lost: %d %v", calls.Load(), err)
	}
	if err := s.SetEnabled(t.Context(), false); err != nil {
		t.Fatal(err)
	}
	if err := s.SetEnabled(t.Context(), true); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Offers(t.Context(), true); err != nil || calls.Load() != 1 {
		t.Fatalf("manual fetch disabled by automatic preference: %d %v", calls.Load(), err)
	}
	var raw string
	if err := db.QueryRow("SELECT value_json FROM app_settings WHERE key='fetch.epic_free'").Scan(&raw); err != nil || raw != "false" {
		t.Fatalf("module switch overwrote automatic preference: %s %v", raw, err)
	}
}
