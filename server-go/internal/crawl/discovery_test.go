package crawl

import (
	"context"
	"testing"

	"holdexar/server-go/internal/storebrowse"
)

func TestDiscoveryUsesStandaloneScheduledJobAndExistingPriceOutputs(t *testing.T) {
	db, gate := runtimeFixture(t)
	priceFixture(t, db, gate)
	called := 0
	r := New(db, gate, nil, Options{Fetch: func(context.Context, []int64, string, string, bool) (map[int64]storebrowse.Item, error) {
		return map[int64]storebrowse.Item{220: {"appid": 220, "success": 1, "visible": true, "type": 0, "name": "验证游戏", "purchase_options": []any{map[string]any{"packageid": "39", "final_price_in_cents": "999", "original_price_in_cents": "1999", "discount_pct": "50"}}}}, nil
	}, AfterJob: func(context.Context, []int64) error { called++; return nil }})
	id, e := r.RunDiscovery(context.Background(), "top100_backfill", []int64{220})
	if e != nil || id == 0 || called != 1 {
		t.Fatalf("%d %d %v", id, called, e)
	}
	var n int
	if e = db.QueryRow(`SELECT count(*) FROM price_cycles`).Scan(&n); e != nil || n != 0 {
		t.Fatalf("unexpected price cycle %d %v", n, e)
	}
	if e = db.QueryRow(`SELECT count(*) FROM crawl_jobs WHERE id=? AND cycle_id IS NULL AND kind='top100_backfill' AND json_extract(stats_json,'$.trigger')='scheduled' AND status='done'`, id).Scan(&n); e != nil || n != 1 {
		t.Fatalf("job %d %v", n, e)
	}
	if e = db.QueryRow(`SELECT count(*) FROM price_events WHERE cycle_id=? AND appid=220`, -id).Scan(&n); e != nil || n == 0 {
		t.Fatalf("outputs %d %v", n, e)
	}
}
