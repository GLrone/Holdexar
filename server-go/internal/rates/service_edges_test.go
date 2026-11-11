package rates

import (
	"context"
	"database/sql"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

func TestHistoricalWriteRollsBackWithDependentFailure(t *testing.T) {
	s, db := serviceFixture(t, ServiceOptions{Revalue: func(ctx context.Context, conn *sql.Conn, _ map[string][]string) (map[string]int, error) {
		_, e := conn.ExecContext(ctx, `INSERT INTO app_settings(key,value_json) VALUES('test.partial','true')`)
		if e != nil {
			return nil, e
		}
		return nil, errors.New("dependent failed")
	}})
	if _, e := db.Exec(`INSERT INTO fx_rate_history(currency_code,rate_date,rate_to_cny,source_kind) VALUES('USD','2026-11-01',9,'carried')`); e != nil {
		t.Fatal(e)
	}
	if _, e := s.writeHistory(context.Background(), map[string]map[string]float64{"2026-11-01": {"USD": 7}, "2026-11-02": {"USD": 7.1}}); e == nil {
		t.Fatal("dependent failure accepted")
	}
	var rate float64
	var count, partial int
	_ = db.QueryRow("SELECT rate_to_cny FROM fx_rate_history WHERE currency_code='USD' AND rate_date='2026-11-01'").Scan(&rate)
	_ = db.QueryRow("SELECT count(*) FROM fx_rate_history").Scan(&count)
	_ = db.QueryRow("SELECT count(*) FROM app_settings WHERE key='test.partial'").Scan(&partial)
	if rate != 9 || count != 1 || partial != 0 {
		t.Fatalf("half state rate=%v rows=%d partial=%d", rate, count, partial)
	}
}

func TestRefreshReusesInFlightRequestWithoutFollowerCancellation(t *testing.T) {
	started, release := make(chan struct{}), make(chan struct{})
	var calls atomic.Int32
	s, _ := serviceFixture(t, ServiceOptions{Fetch: func(ctx context.Context, _ string, _ time.Duration) ([]byte, error) {
		calls.Add(1)
		close(started)
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-release:
			return []byte(`{"USD":7,"EUR":8,"CNY":1,"JPY":0.05,"TRY":0.2}`), nil
		}
	}})
	owner := make(chan error, 1)
	go func() { _, e := s.Refresh(context.Background()); owner <- e }()
	<-started
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if _, e := s.Refresh(ctx); !errors.Is(e, context.DeadlineExceeded) {
		t.Fatal(e)
	}
	close(release)
	if e := <-owner; e != nil {
		t.Fatal(e)
	}
	if calls.Load() != 1 {
		t.Fatalf("duplicate requests=%d", calls.Load())
	}
}

func TestRepairDryRunWindowLimitAndConcurrentFetch(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	started, release := make(chan string, 2), make(chan struct{})
	now := time.Date(2026, 11, 6, 12, 0, 0, 0, time.UTC)
	s, db := serviceFixture(t, ServiceOptions{Now: func() time.Time { return now }, Fetch: func(ctx context.Context, url string, _ time.Duration) ([]byte, error) {
		started <- url
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-release:
			return []byte(`[]`), nil
		}
	}})
	if _, e := db.Exec(`INSERT INTO fx_rate_history(currency_code,rate_date,rate_to_cny,source_kind) VALUES('USD','2025-01-01',7,'observed')`); e != nil {
		t.Fatal(e)
	}
	limit := 2
	dry, e := s.Repair(ctx, true, &limit)
	if e != nil || dry["status"] != "dry_run" || len(dry["planned"].([]gapWindow)) != 2 {
		t.Fatalf("dry=%v err=%v", dry, e)
	}
	select {
	case <-started:
		t.Fatal("dry run fetched")
	default:
	}
	done := make(chan error, 1)
	go func() { _, e := s.Repair(ctx, false, &limit); done <- e }()
	for i := 0; i < 2; i++ {
		select {
		case <-started:
		case <-ctx.Done():
			t.Fatal("window requests were serialized")
		}
	}
	close(release)
	if e := <-done; e != nil {
		t.Fatal(e)
	}
}

func TestRateWriteRoutesUseActualHTTPAndPreserveFailureStatus(t *testing.T) {
	var fail atomic.Bool
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if fail.Load() {
			w.WriteHeader(503)
			return
		}
		_, _ = w.Write([]byte(`{"USD":7,"EUR":8,"CNY":1,"JPY":0.05,"TRY":0.2}`))
	}))
	defer upstream.Close()
	s, db := serviceFixture(t, ServiceOptions{Fetch: func(ctx context.Context, _ string, timeout time.Duration) ([]byte, error) {
		return fetchJSON(ctx, upstream.URL, timeout)
	}})
	mux := http.NewServeMux()
	Register(mux, db, s)
	call := func(path string) *httptest.ResponseRecorder {
		t.Helper()
		w := httptest.NewRecorder()
		mux.ServeHTTP(w, httptest.NewRequest("POST", path, nil))
		return w
	}
	if w := call("/api/v1/rates/refresh"); w.Code != 200 {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	if w := call("/api/v1/rates/history/repair?dry_run=maybe"); w.Code != 422 {
		t.Fatalf("bool status=%d", w.Code)
	}
	if w := call("/api/v1/rates/history/repair?max_windows=invalid"); w.Code != 422 {
		t.Fatalf("integer status=%d", w.Code)
	}
	fail.Store(true)
	if w := call("/api/v1/rates/refresh"); w.Code != 502 {
		t.Fatalf("failure=%d %s", w.Code, w.Body.String())
	}
}

func TestRefreshPanicReleasesInFlightSlot(t *testing.T) {
	s, _ := serviceFixture(t, ServiceOptions{Fetch: func(context.Context, string, time.Duration) ([]byte, error) { panic("controlled") }})
	func() {
		defer func() {
			if recover() == nil {
				t.Error("expected panic")
			}
		}()
		_, _ = s.Refresh(context.Background())
	}()
	s.opts.Fetch = func(context.Context, string, time.Duration) ([]byte, error) {
		return []byte(`{"USD":7,"EUR":8,"CNY":1,"JPY":0.05,"TRY":0.2}`), nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if _, e := s.Refresh(ctx); e != nil {
		t.Fatalf("refresh remained stuck after panic: %v", e)
	}
}
