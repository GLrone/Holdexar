package alerts

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestEventsPreservesDetailsCurrencyOverRegionMapping(t *testing.T) {
	s, db := fixture(t)
	if _, e := db.Exec(`INSERT INTO games(appid,name,family_sharing,trading_cards,is_epic,is_hb,hl_flag,pp_flag,diff_fen,is_adult,is_visual_novel,removed_strikes,positive_reviews,review_count,view_count) VALUES(620,'传送门2',0,0,0,0,0,0,0,0,0,0,0,0,0);
		INSERT INTO alert_events(alert_id,appid,region,price,price_cny,triggered_at,notified,details_json) VALUES(1,620,'TR',1299,7000,'2026-11-06 10:00:00.000000',0,'{"currency":"USD"}')`); e != nil {
		t.Fatal(e)
	}
	mux := http.NewServeMux()
	s.Register(mux)
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/v1/alerts/events", nil))
	if rec.Code != 200 {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	var got []map[string]any
	if e := json.Unmarshal(rec.Body.Bytes(), &got); e != nil {
		t.Fatal(e)
	}
	if len(got) != 1 || got[0]["priceText"] != "$12.99" {
		t.Fatalf("expected $12.99 from details.currency, got %+v", got)
	}
}

func TestCheckBundleDealsFiltersThresholdCooldownIdempotencyAndDispatches(t *testing.T) {
	now := time.Date(2026, 11, 6, 11, 0, 0, 0, time.UTC)
	dispatched := 0
	s, db := fixture(t)
	s.opts.Now = func() time.Time { return now }
	s.opts.Dispatch = func(context.Context) error { dispatched++; return nil }

	_, e := db.Exec(`
		INSERT INTO app_settings(key,value_json) VALUES
			('notifications.prefs','{"enabled":true}'),
			('smtp.to_addr','"capture@localhost"');
		INSERT INTO bundles(bundle_id,name,must_purchase_as_set,item_kind,is_lowest,view_count) VALUES
			(101,'合格大包',0,0,1,0),
			(102,'低价小包',0,0,1,0),
			(103,'低折扣包',0,0,1,0);
		INSERT INTO bundle_region_prices(bundle_id,region_code,currency,price,original_price,discount_percent,bundle_base_discount,price_status,cny_fen,app_ids) VALUES
			(101,'CN','CNY',2000,4000,50,50,'ok',2000,'[1,2,3]'),
			(101,'US','USD',500,2000,75,75,'ok',3600,'[1,2,3]'),
			(102,'CN','CNY',300,1500,80,80,'ok',300,'[4]'),
			(103,'CN','CNY',1000,1400,29,29,'ok',1000,'[5]');
	`)
	if e != nil {
		t.Fatal(e)
	}
	ctx := context.Background()
	fresh, e := s.CheckBundleDeals(ctx)
	if e != nil || len(fresh) != 1 || dispatched != 1 {
		t.Fatalf("first run: fresh=%+v dispatched=%d err=%v", fresh, dispatched, e)
	}
	if fresh[0]["bundle_id"] != int64(101) || fresh[0]["max_discount"] != int64(75) || fresh[0]["cn_price_text"] != "¥20.00" {
		t.Fatalf("unexpected pick: %+v", fresh[0])
	}
	var factKey string
	var candidates int
	if e := db.QueryRow(`SELECT fact_key FROM fact_notices WHERE source='bundles' AND kind='discount_pick'`).Scan(&factKey); e != nil || factKey != "bundle:101:75" {
		t.Fatalf("fact_key=%q err=%v", factKey, e)
	}
	db.QueryRow(`SELECT COUNT(*) FROM notification_candidates WHERE source_kind='fact_notice' AND event_type='BUNDLE_DEAL'`).Scan(&candidates)
	if candidates != 1 {
		t.Fatalf("candidates=%d", candidates)
	}

	now = now.Add(19 * time.Hour)
	fresh, e = s.CheckBundleDeals(ctx)
	if e != nil || len(fresh) != 0 || dispatched != 1 {
		t.Fatalf("cooldown failed: fresh=%+v dispatched=%d err=%v", fresh, dispatched, e)
	}

	now = now.Add(2 * time.Hour)
	fresh, e = s.CheckBundleDeals(ctx)
	if e != nil || len(fresh) != 0 || dispatched != 1 {
		t.Fatalf("idempotency failed: fresh=%+v dispatched=%d err=%v", fresh, dispatched, e)
	}

	if _, e := db.Exec(`UPDATE bundle_region_prices SET discount_percent=85 WHERE bundle_id=101 AND region_code='US'`); e != nil {
		t.Fatal(e)
	}
	now = now.Add(21 * time.Hour)
	fresh, e = s.CheckBundleDeals(ctx)
	if e != nil || len(fresh) != 1 || dispatched != 2 || fresh[0]["max_discount"] != int64(85) {
		t.Fatalf("deeper discount failed: fresh=%+v dispatched=%d err=%v", fresh, dispatched, e)
	}
}
