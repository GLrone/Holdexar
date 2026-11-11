package crawl

import (
	"context"
	"fmt"
	"sync"
	"testing"
	"time"

	"holdexar/server-go/internal/monitoring"
	"holdexar/server-go/internal/storebrowse"
)

func TestAccountDiscoverySplitsRegionsAndKeepsOverlapGlobal(t *testing.T) {
	db, gate := runtimeFixture(t)
	ctx := context.Background()
	monitor := monitoring.New(db, gate)
	if _, e := monitor.Apply(ctx, "game", []int64{220, 221, 222}, monitoring.Change{AddOnly: true, Sources: map[int64][]string{220: {"family_wishlist"}, 221: {"owned"}, 222: {"owned"}}}); e != nil {
		t.Fatal(e)
	}
	on := true
	if _, e := monitor.Apply(ctx, "game", []int64{222}, monitoring.Change{Excluded: &on}); e != nil {
		t.Fatal(e)
	}
	if _, e := db.Exec(`INSERT INTO app_settings(key,value_json) VALUES('crawl.owned_regions','["us"]') ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json`); e != nil {
		t.Fatal(e)
	}
	var calls []string
	r := New(db, gate, nil, Options{Fetch: func(_ context.Context, ids []int64, cc, lang string, metadata bool) (map[int64]storebrowse.Item, error) {
		if !metadata {
			calls = append(calls, fmt.Sprint(ids)+"/"+cc)
		}
		items := map[int64]storebrowse.Item{}
		for _, id := range ids {
			items[id] = storebrowse.Item{"appid": id, "success": 1, "visible": true, "type": 0, "name": "fixture", "purchase_options": []any{map[string]any{"packageid": "39", "final_price_in_cents": "999", "original_price_in_cents": "1999", "discount_pct": "50"}}}
		}
		return items, nil
	}})
	started, e := r.RunAccountDiscovery(ctx, []int64{220, 220}, []int64{220, 221, 222, 999})
	if e != nil || !started || fmt.Sprint(calls) != "[[220]/cn [221]/us]" {
		t.Fatalf("plans %t %v %v", started, calls, e)
	}
	var jobs, cycles int
	db.QueryRow(`SELECT COUNT(*) FROM crawl_jobs WHERE kind='wishlist_sync' AND status='done' AND cycle_id IS NULL`).Scan(&jobs)
	db.QueryRow(`SELECT COUNT(*) FROM price_cycles`).Scan(&cycles)
	if jobs != 2 || cycles != 0 {
		t.Fatalf("parallel ledgers %d %d", jobs, cycles)
	}
	if _, e := db.Exec(`INSERT INTO wishlist_items(steamid,appid,active,owned,wishlisted,manual,excluded,manual_pool,board_pool) VALUES('other',221,1,0,1,0,0,0,0)`); e != nil {
		t.Fatal(e)
	}
	calls = nil
	if _, e = r.RunAccountDiscovery(ctx, nil, []int64{221}); e != nil || fmt.Sprint(calls) != "[[221]/cn]" {
		t.Fatalf("another account's wishlist lost global regions %v %v", calls, e)
	}
	if _, e := db.Exec(`DELETE FROM app_settings WHERE key='crawl.owned_regions'`); e != nil {
		t.Fatal(e)
	}
	calls = nil
	if _, e = r.RunAccountDiscovery(ctx, []int64{220}, []int64{221}); e != nil || fmt.Sprint(calls) != "[[220 221]/cn]" {
		t.Fatalf("follow mode %v %v", calls, e)
	}
}
func TestAccountDiscoveryBackgroundReservationBusyAndClose(t *testing.T) {
	db, gate := runtimeFixture(t)
	ctx := context.Background()
	priceFixture(t, db, gate)
	entered := make(chan struct{})
	var once sync.Once
	r := New(db, gate, nil, Options{Fetch: func(ctx context.Context, _ []int64, _, _ string, _ bool) (map[int64]storebrowse.Item, error) {
		once.Do(func() { close(entered) })
		<-ctx.Done()
		return nil, ctx.Err()
	}})
	if started, e := r.StartAccountDiscovery(ctx, []int64{220}, nil); e != nil || !started {
		t.Fatalf("start %t %v", started, e)
	}
	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("background absent")
	}
	if _, e := r.StartAccountDiscovery(ctx, []int64{220}, nil); e != ErrBusy {
		t.Fatal("busy lost", e)
	}
	end, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	if e := r.Close(end); e != nil {
		t.Fatal(e)
	}
	var running int
	db.QueryRow(`SELECT COUNT(*) FROM crawl_jobs WHERE status='running'`).Scan(&running)
	if running != 0 {
		t.Fatalf("closing orphaned job %d", running)
	}
	if _, e := r.StartAccountDiscovery(ctx, []int64{220}, nil); e != context.Canceled {
		t.Fatal("closed accepted", e)
	}
}
