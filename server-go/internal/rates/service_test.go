package rates

import (
	"context"
	"database/sql"
	"errors"
	"path/filepath"
	"testing"
	"time"

	"holdexar/server-go/internal/crawler/dbwriter"
	"holdexar/server-go/internal/store"
)

func serviceFixture(t *testing.T, opts ServiceOptions) (*Service, *sql.DB) {
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
	return NewService(db, store.NewWriteGate(db, path), opts), db
}

func TestRefreshAtomicRatesCurrentPricesAndSnapshots(t *testing.T) {
	ctx := context.Background()
	now := time.Date(2026, 11, 6, 9, 0, 0, 0, time.FixedZone("Beijing", 8*3600))
	s, db := serviceFixture(t, ServiceOptions{Now: func() time.Time { return now }, Fetch: func(context.Context, string, time.Duration) ([]byte, error) {
		return []byte(`{"USD":{"CNY":7},"EUR":8,"JPY":0.05,"CNY":9,"TRY":0.2,"XXX":42}`), nil
	}})
	name, kind, region, currency := "test", "GAME", "US", "USD"
	price, sub := int64(1000), int64(39)
	writer := dbwriter.NewWriter(db, s.Gate)
	if _, e := writer.UpsertTaskBatch(ctx, []dbwriter.BatchEntry{{Game: dbwriter.GameData{AppID: 220, Name: &name, Type: &kind}, Prices: []dbwriter.PriceRow{{AppID: 220, RegionCode: &region, Currency: &currency, Price: &price, OriginalPrice: &price, SubID: &sub}}}}); e != nil {
		t.Fatal(e)
	}
	if _, e := db.Exec(`UPDATE game_current_prices SET cny_fen=1; UPDATE game_price_history SET cny_fen=1234; INSERT INTO fx_rates(currency_code,rate_to_cny) VALUES('XXX',2);`); e != nil {
		t.Fatal(e)
	}
	result, e := s.Refresh(ctx)
	if e != nil {
		t.Fatal(e)
	}
	if result["count"] != 5 || result["source"] != "augmentedsteam" {
		t.Fatal(result)
	}
	var rate float64
	var current, history, minimum, extra int
	if e = db.QueryRow("SELECT rate_to_cny FROM fx_rates WHERE currency_code='USD'").Scan(&rate); e != nil {
		t.Fatal(e)
	}
	_ = db.QueryRow("SELECT cny_fen FROM game_current_prices WHERE appid=220").Scan(&current)
	_ = db.QueryRow("SELECT cny_fen FROM game_price_history WHERE appid=220").Scan(&history)
	_ = db.QueryRow("SELECT min_cny_fen FROM games WHERE appid=220").Scan(&minimum)
	_ = db.QueryRow("SELECT count(*) FROM fx_rates WHERE currency_code='XXX'").Scan(&extra)
	if rate != 7 || current != 7000 || minimum != 7000 || history != 1234 || extra != 0 {
		t.Fatalf("rate=%v current=%d min=%d history=%d extra=%d", rate, current, minimum, history, extra)
	}
	if _, e = s.Refresh(ctx); e != nil {
		t.Fatal(e)
	}
	var daily int
	_ = db.QueryRow("SELECT count(*) FROM fx_rate_history WHERE currency_code='USD' AND rate_date='2026-11-06'").Scan(&daily)
	if daily != 1 {
		t.Fatalf("daily=%d", daily)
	}
	if _, e = db.Exec(`CREATE TRIGGER reject_derived BEFORE UPDATE ON games BEGIN SELECT RAISE(ABORT,'controlled failure'); END`); e != nil {
		t.Fatal(e)
	}
	s.opts.Fetch = func(context.Context, string, time.Duration) ([]byte, error) {
		return []byte(`{"USD":9,"EUR":8,"JPY":0.05,"CNY":1,"TRY":0.2}`), nil
	}
	if _, e = s.Refresh(ctx); e == nil {
		t.Fatal("derivation failure accepted")
	}
	_ = db.QueryRow("SELECT rate_to_cny FROM fx_rates WHERE currency_code='USD'").Scan(&rate)
	if rate != 7 {
		t.Fatalf("half refreshed rate=%v", rate)
	}
}

func TestCurrentFallbackStalenessAndDisabledAutomaticRefresh(t *testing.T) {
	calls := []string{}
	now := time.Date(2026, 11, 6, 9, 0, 0, 0, time.UTC)
	s, db := serviceFixture(t, ServiceOptions{Now: func() time.Time { return now }, Fetch: func(_ context.Context, url string, _ time.Duration) ([]byte, error) {
		calls = append(calls, url)
		if url == AugmentedURL {
			return nil, errors.New("unavailable")
		}
		return []byte(`{"rates":{"USD":0.125,"EUR":0.1,"CNY":1}}`), nil
	}})
	if changed, e := s.RefreshIfStale(context.Background()); e != nil || !changed {
		t.Fatalf("changed=%v err=%v", changed, e)
	}
	if len(calls) != 3 {
		t.Fatalf("expected two primary attempts then fallback: %v", calls)
	}
	if changed, e := s.RefreshIfStale(context.Background()); e != nil || changed {
		t.Fatalf("fresh=%v err=%v", changed, e)
	}
	if _, e := db.Exec(`UPDATE fx_rates SET fetched_at='2000-01-01';INSERT INTO app_settings(key,value_json) VALUES('crawl.auto_refresh_rates','false')`); e != nil {
		t.Fatal(e)
	}
	if changed, e := s.RefreshIfStale(context.Background()); e != nil || changed || len(calls) != 3 {
		t.Fatalf("disabled changed=%v calls=%v err=%v", changed, calls, e)
	}
	if _, e := s.Refresh(context.Background()); e != nil {
		t.Fatal("manual must work while automatic disabled", e)
	}
}

func TestHistoricalRepairProtectsObservedAndRevaluesOnlyTouchedDays(t *testing.T) {
	ctx := context.Background()
	now := time.Date(2026, 11, 6, 9, 0, 0, 0, time.UTC)
	var touched map[string][]string
	s, db := serviceFixture(t, ServiceOptions{Now: func() time.Time { return now }, Fetch: func(context.Context, string, time.Duration) ([]byte, error) {
		return []byte(`[{"date":"2026-11-03","quote":"CNY","rate":7},{"date":"2026-11-03","quote":"EUR","rate":0.875},{"date":"2026-11-04","quote":"CNY","rate":7.1},{"date":"2026-11-04","quote":"EUR","rate":0.8875}]`), nil
	}, Revalue: func(_ context.Context, _ *sql.Conn, t map[string][]string) (map[string]int, error) {
		touched = t
		return map[string]int{"bills": 1, "transactions": 2}, nil
	}})
	if _, e := db.Exec(`INSERT INTO fx_rate_history(currency_code,rate_date,rate_to_cny,source_kind,source) VALUES('USD','2026-11-03',9,'observed','augmentedsteam'),('USD','2026-11-04',9,'carried','carry'),('USD','2026-11-05',9,'observed','augmentedsteam'),('EUR','2026-11-03',9,'observed','bing.currencyapi'),('EUR','2026-11-05',8,'derived','frankfurter.v2')`); e != nil {
		t.Fatal(e)
	}
	maxWindows := 1
	result, e := s.Repair(ctx, false, &maxWindows)
	if e != nil {
		t.Fatal(e)
	}
	if result["status"] != "ok" || result["requests"] != 1 {
		t.Fatal(result)
	}
	var rate float64
	_ = db.QueryRow("SELECT rate_to_cny FROM fx_rate_history WHERE currency_code='USD' AND rate_date='2026-11-03'").Scan(&rate)
	if rate != 9 {
		t.Fatalf("observed was replaced=%v", rate)
	}
	_ = db.QueryRow("SELECT rate_to_cny FROM fx_rate_history WHERE currency_code='USD' AND rate_date='2026-11-04'").Scan(&rate)
	if rate != 7.1 || len(touched["USD"]) != 1 || touched["USD"][0] != "2026-11-04" {
		t.Fatalf("rate=%v touched=%v", rate, touched)
	}
	_ = db.QueryRow("SELECT rate_to_cny FROM fx_rate_history WHERE currency_code='EUR' AND rate_date='2026-11-03'").Scan(&rate)
	if rate != 8 {
		t.Fatalf("cross rate=%v", rate)
	}
}
