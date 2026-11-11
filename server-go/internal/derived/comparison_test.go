package derived

import (
	"context"
	"holdexar/server-go/internal/store"
	"path/filepath"
	"testing"
	"time"
)

func TestHistoryContextUsesOnlyComparableEvidenceBeforeObservation(t *testing.T) {
	db, e := store.Open(filepath.Join(t.TempDir(), "holdexar.db"))
	if e != nil {
		t.Fatal(e)
	}
	defer db.Close()
	ctx := context.Background()
	if e = store.InitializeFreshDatabase(ctx, db); e != nil {
		t.Fatal(e)
	}
	_, e = db.Exec(`INSERT INTO game_price_history(appid,region_code,currency,price,discount_percent,sub_id,is_gold,is_bundle,price_status,source,snapshot_at) VALUES
 (620,'US','USD',1999,0,400,0,0,'ok','steam','2026-11-05 10:00:00.000000'),
 (620,'US','USD',999,0,400,0,0,'ok','steam','2026-11-06 10:00:00.000000'),
 (620,'US','USD',1,0,401,0,0,'ok','steam','2026-11-04 10:00:00.000000'),
 (620,'US','USD',2,0,400,1,0,'ok','steam','2026-11-04 10:00:00.000000'),
 (620,'US','CNY',3,0,400,0,0,'ok','steam','2026-11-04 10:00:00.000000');
 INSERT INTO game_current_prices(appid,region_code,currency,price,discount_percent,sub_id,price_status,fail_count,attempt_outcome,steam_answer,last_success_at,updated_at) VALUES(620,'US','USD',999,0,400,'ok',0,'success','ok','2026-11-06 10:00:00.000000','2026-11-06 10:00:00.000000')`)
	if e != nil {
		t.Fatal(e)
	}
	cur := map[string]any{"appid": int64(620), "region": "US", "currency": "USD", "subId": int64(400), "price": int64(999), "observedAt": "2026-11-06T10:00:00"}
	got, e := HistoryContext(ctx, db, []map[string]any{cur}, time.Now(), nil)
	if e != nil {
		t.Fatal(e)
	}
	h := got[ComparisonKey(cur)]
	if h["baselineLow"] != int64(1999) || h["currentKnown"] != true || h["points"] != int64(1) {
		t.Fatalf("%#v", h)
	}
	_, e = db.Exec(`UPDATE game_current_prices SET attempt_outcome='failed'`)
	if e != nil {
		t.Fatal(e)
	}
	got, e = HistoryContext(ctx, db, []map[string]any{cur}, time.Now(), nil)
	if e != nil {
		t.Fatal(e)
	}
	if got[ComparisonKey(cur)]["currentKnown"] != false {
		t.Fatal("failed latest attempt remained authoritative")
	}
}
