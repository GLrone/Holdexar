package alerts

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestFeaturedPreviewAndAutomaticFactsShareReliableObservation(t *testing.T) {
	s, db := fixture(t)
	observed(t, db)
	ctx := context.Background()
	if _, e := db.Exec(`INSERT INTO monitor_sources(target_type,target_id,source,priority,active) VALUES('game',620,'favorite',100,1);UPDATE app_settings SET value_json='{"enabled":true,"manualRegion":"US"}' WHERE key='notifications.prefs'`); e != nil {
		t.Fatal(e)
	}
	preview, e := s.PreviewFeatured(ctx, map[string]any{"region": "US"})
	if e != nil || preview["matched"] != 1 || preview["total"] != 1 {
		t.Fatalf("%v %v", preview, e)
	}
	var n int
	db.QueryRow(`SELECT COUNT(*) FROM alert_events`).Scan(&n)
	if n != 0 {
		t.Fatal("preview wrote events")
	}
	if n, e := s.CheckFeatured(ctx, []int64{620}); e != nil || n != 1 {
		t.Fatalf("%d %v", n, e)
	}
	if n, e := s.CheckFeatured(ctx, nil); e != nil || n != 0 {
		t.Fatalf("duplicate %d %v", n, e)
	}
	if _, e := db.Exec(`UPDATE game_current_prices SET updated_at='2026-11-06 10:01:00.000000',last_success_at='2026-11-06 10:01:00.000000'`); e != nil {
		t.Fatal(e)
	}
	if n, e := s.CheckFeatured(ctx, nil); e != nil || n != 0 {
		t.Fatalf("same price observation duplicated %d %v", n, e)
	}
	db.QueryRow(`SELECT COUNT(*) FROM price_alerts`).Scan(&n)
	if n != 0 {
		t.Fatal("featured created a saved rule")
	}
	db.QueryRow(`SELECT COUNT(*) FROM notification_candidates WHERE source_kind='alert_event' AND event_type='FEATURED'`).Scan(&n)
	if n != 1 {
		t.Fatal(n)
	}
}

func TestFeaturedUnknownRegionIsExplicitAndDoesNotCreateFacts(t *testing.T) {
	s, db := fixture(t)
	observed(t, db)
	result, e := s.PreviewFeatured(context.Background(), map[string]any{})
	if e != nil || result["total"] != 0 {
		t.Fatalf("%v %v", result, e)
	}
	exclusions := result["exclusions"].([]map[string]any)
	if len(exclusions) != 1 || exclusions[0]["code"] != "region_unknown" {
		t.Fatal(result)
	}
	if n, e := s.CheckFeatured(context.Background(), nil); e != nil || n != 0 {
		t.Fatalf("%d %v", n, e)
	}
}

func TestFeaturedSortsMatchedFirstAndRespectsMembership(t *testing.T) {
	s, db := fixture(t)
	observed(t, db)
	ctx := context.Background()
	_, e := db.Exec(`INSERT INTO games(appid,name,family_sharing,trading_cards,is_epic,is_hb,hl_flag,pp_flag,diff_fen,is_adult,is_visual_novel,removed_strikes,positive_rate,positive_reviews,review_count,view_count) VALUES(621,'second',0,0,0,0,0,0,0,0,0,0,9500,100,100,0);
 UPDATE games SET positive_rate=9800,review_count=100 WHERE appid=620;
 INSERT INTO game_price_history(appid,region_code,currency,price,discount_percent,sub_id,is_gold,is_bundle,price_status,source,snapshot_at) VALUES(621,'US','USD',500,75,401,0,0,'ok','steam','2026-11-06 10:00:00.000000');
 INSERT INTO game_current_prices(appid,region_code,currency,price,original_price,discount_percent,sub_id,price_status,fail_count,attempt_outcome,steam_answer,last_success_at,updated_at) VALUES(621,'US','USD',500,2000,75,401,'ok',0,'success','ok','2026-11-06 10:00:00.000000','2026-11-06 10:00:00.000000');
 UPDATE game_current_prices SET original_price=1999 WHERE appid=620;
 INSERT INTO monitor_sources(target_type,target_id,source,priority,active) VALUES('game',620,'favorite',100,1),('game',621,'family_wishlist',50,1)`)
	if e != nil {
		t.Fatal(e)
	}
	for sortKey, first := range map[string]int64{"discount": 621, "price": 621, "savings": 621, "quality": 620, "followed": 620, "wishlist": 621} {
		preview, e := s.PreviewFeatured(ctx, map[string]any{"region": "US", "featured": map[string]any{"sort": sortKey}})
		if e != nil {
			t.Fatal(e)
		}
		items := preview["items"].([]map[string]any)
		if len(items) != 2 || obj(items[0]["observed"])["appid"] != first {
			t.Fatalf("%s %v", sortKey, items)
		}
	}
	_, e = db.Exec(`INSERT INTO monitor_exclusions(target_type,target_id,active) VALUES('game',621,1)`)
	if e != nil {
		t.Fatal(e)
	}
	preview, e := s.PreviewFeatured(ctx, map[string]any{"region": "US"})
	if e != nil || preview["matched"] != 1 || obj(preview["items"].([]map[string]any)[0]["observed"])["appid"] != int64(620) {
		t.Fatalf("%v %v", preview, e)
	}
}

func TestFeaturedPreviewHTTPUsesExistingContract(t *testing.T) {
	s, db := fixture(t)
	observed(t, db)
	mux := http.NewServeMux()
	s.Register(mux)
	request := func(body string) (int, map[string]any) {
		w := httptest.NewRecorder()
		mux.ServeHTTP(w, httptest.NewRequest("POST", "/api/v1/alerts/featured/preview", strings.NewReader(body)))
		var value map[string]any
		if e := json.Unmarshal(w.Body.Bytes(), &value); e != nil {
			t.Fatal(w.Body.String())
		}
		return w.Code, value
	}
	code, value := request(`{"region":"US","featured":{"scope":["catalog"],"minDiscount":50}}`)
	if code != 200 || value["matched"] != float64(1) {
		t.Fatalf("%d %v", code, value)
	}
	code, _ = request(`{"region":"invalid"}`)
	if code != 400 {
		t.Fatal(code)
	}
	code, value = request(`{"region":"US","appids":[],"featured":{"scope":["catalog"]}}`)
	if code != 200 || value["total"] != float64(0) {
		t.Fatalf("%d %v", code, value)
	}
}
