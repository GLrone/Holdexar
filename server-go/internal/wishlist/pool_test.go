package wishlist

import (
	"context"
	"database/sql"
	"holdexar/server-go/internal/crawler/dbwriter"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"

	"holdexar/server-go/internal/monitoring"
	"holdexar/server-go/internal/store"
)

func poolFixture(t *testing.T) (*Pool, *sql.DB) {
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
	gate := store.NewWriteGate(db, path)
	return NewPool(db, gate, monitoring.New(db, gate)), db
}

func TestPoolAddWithoutAccountAndConcurrentIdempotency(t *testing.T) {
	p, db := poolFixture(t)
	ctx := context.Background()
	var called atomic.Int32
	p.OnAdded = func(context.Context, []int64) (bool, error) { called.Add(1); return true, nil }
	var wg sync.WaitGroup
	results := make(chan map[string]any, 2)
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			r, e := p.Add(ctx, []int64{220, 220, -1}, "名单.json")
			if e != nil {
				t.Error(e)
				return
			}
			results <- r
		}()
	}
	wg.Wait()
	close(results)
	added, exists := 0, 0
	for r := range results {
		added += r["added"].(int)
		exists += r["exists"].(int)
		if r["fail"] != 1 {
			t.Fatalf("invalid item lost=%v", r)
		}
	}
	if added != 1 || exists != 1 || called.Load() != 1 {
		t.Fatalf("added=%d exists=%d first-crawls=%d", added, exists, called.Load())
	}
	var accounts, rows int
	var source string
	_ = db.QueryRow("SELECT count(*) FROM wishlist_items").Scan(&rows)
	_ = db.QueryRow("SELECT count(*) FROM tracked_accounts").Scan(&accounts)
	_ = db.QueryRow("SELECT source FROM preset_games WHERE appid=220").Scan(&source)
	if accounts != 0 || rows != 0 || source != "file:名单.json" {
		t.Fatalf("accounts=%d wishlist=%d source=%s", accounts, rows, source)
	}
}

func TestUnfollowPreservesAccountAndRemovalCannotBeRevivedByAccount(t *testing.T) {
	p, db := poolFixture(t)
	ctx := context.Background()
	if _, e := db.Exec(`INSERT INTO wishlist_items VALUES('a',220,NULL,1,1,1,1,0,0,0),('b',220,NULL,1,0,0,1,0,0,0)`); e != nil {
		t.Fatal(e)
	}
	if _, e := p.Monitor.Apply(ctx, "game", []int64{220}, monitoring.Change{Sources: map[int64][]string{220: {"owned", "family_wishlist", "favorite"}}, AddOnly: true}); e != nil {
		t.Fatal(e)
	}
	if _, e := p.Follow(ctx, 220, false); e != nil {
		t.Fatal(e)
	}
	snapshot, e := p.Monitor.Snapshot(ctx, "game", 220)
	if e != nil {
		t.Fatal(e)
	}
	if snapshot["state"] != "active" || len(snapshot["sources"].([]string)) != 2 {
		t.Fatalf("unfollow lost account sources=%v", snapshot)
	}
	if _, e := p.Remove(ctx, []int64{220}); e != nil {
		t.Fatal(e)
	}
	if _, e := p.Monitor.Apply(ctx, "game", []int64{220}, monitoring.Change{Sources: map[int64][]string{220: {"owned", "family_wishlist"}}, Managed: []string{"owned", "family_wishlist"}}); e != nil {
		t.Fatal(e)
	}
	snapshot, _ = p.Monitor.Snapshot(ctx, "game", 220)
	if snapshot["state"] != "excluded" {
		t.Fatalf("sync resurrected removed=%v", snapshot)
	}
	r, e := p.Add(ctx, []int64{220}, "")
	if e != nil {
		t.Fatal(e)
	}
	if r["restored"] != 1 {
		t.Fatalf("restore=%v", r)
	}
	var active, excluded, manual int
	if e := db.QueryRow("SELECT sum(active),sum(excluded),sum(manual) FROM wishlist_items WHERE appid=220").Scan(&active, &excluded, &manual); e != nil {
		t.Fatal(e)
	}
	if active != 0 || excluded != 2 || manual != 0 {
		t.Fatal("local re-add rewrote account source rows")
	}
}

func TestRemoveFailureRollsBackAccountAndMonitoringTogether(t *testing.T) {
	p, db := poolFixture(t)
	ctx := context.Background()
	if _, e := p.Add(ctx, []int64{220}, ""); e != nil {
		t.Fatal(e)
	}
	if _, e := db.Exec(`INSERT INTO wishlist_items VALUES('a',220,NULL,1,1,0,1,0,0,0); CREATE TRIGGER reject_exclusion BEFORE INSERT ON monitor_exclusions BEGIN SELECT RAISE(ABORT,'controlled failure'); END`); e != nil {
		t.Fatal(e)
	}
	if _, e := p.Remove(ctx, []int64{220}); e == nil {
		t.Fatal("remove failure swallowed")
	}
	var active int
	_ = db.QueryRow("SELECT active FROM wishlist_items WHERE steamid='a' AND appid=220").Scan(&active)
	snapshot, _ := p.Monitor.Snapshot(ctx, "game", 220)
	if active != 1 || snapshot["state"] != "active" {
		t.Fatalf("partial removal active=%d state=%v", active, snapshot)
	}
}

func TestPermanentFreeReleasePreservesPromotionsAndDoesNotCreateUserExclusion(t *testing.T) {
	p, db := poolFixture(t)
	ctx := context.Background()
	if _, e := p.Add(ctx, []int64{1, 2}, ""); e != nil {
		t.Fatal(e)
	}
	name, typ, cc, currency := "free test", "GAME", "CN", "CNY"
	zero, paid := int64(0), int64(100)
	writer := dbwriter.NewWriter(db, p.Gate)
	entries := []dbwriter.BatchEntry{{Game: dbwriter.GameData{AppID: 1, Name: &name, Type: &typ}, Prices: []dbwriter.PriceRow{{AppID: 1, RegionCode: &cc, Currency: &currency, Price: &zero, OriginalPrice: &zero}}}, {Game: dbwriter.GameData{AppID: 2, Name: &name, Type: &typ}, Prices: []dbwriter.PriceRow{{AppID: 2, RegionCode: &cc, Currency: &currency, Price: &zero, OriginalPrice: &paid}}}}
	if _, e := writer.UpsertTaskBatch(ctx, entries); e != nil {
		t.Fatal(e)
	}
	if _, e := p.ReleaseFree(ctx, []int64{1, 2}); e != nil {
		t.Fatal(e)
	}
	one, e := p.Monitor.Snapshot(ctx, "game", 1)
	if e != nil {
		t.Fatal(e)
	}
	two, e := p.Monitor.Snapshot(ctx, "game", 2)
	if e != nil {
		t.Fatal(e)
	}
	if one["state"] != "released" || one["excluded"] != false || two["state"] != "active" {
		t.Fatalf("f2p=%v promo=%v", one, two)
	}
}

func TestPoolItemsMergeAccountFlagsAndKeepAccountFilters(t *testing.T) {
	p, db := poolFixture(t)
	ctx := context.Background()
	if _, err := db.Exec(`INSERT INTO wishlist_items VALUES('a',220,NULL,1,1,0,0,0,0,0),('b',220,NULL,1,0,1,1,0,0,0),('a',221,NULL,0,0,0,1,0,0,0)`); err != nil {
		t.Fatal(err)
	}
	if _, err := p.Monitor.Apply(ctx, "game", []int64{220, 620}, monitoring.Change{Sources: map[int64][]string{220: {"favorite"}, 620: {"manual", "board"}}, AddOnly: true}); err != nil {
		t.Fatal(err)
	}
	all, err := p.Items(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(all) != 2 || all[0]["appid"] != int64(220) || all[0]["owned"] != true || all[0]["followed"] != true || all[0]["wishlisted"] != true || len(all[0]["steamids"].([]string)) != 2 || all[1]["manualPool"] != true || all[1]["boardPool"] != true {
		t.Fatalf("merged=%v", all)
	}
	account := "a"
	filtered, err := p.Items(ctx, &account)
	if err != nil {
		t.Fatal(err)
	}
	if len(filtered) != 1 || filtered[0]["owned"] != true || filtered[0]["followed"] != false || filtered[0]["wishlisted"] != false {
		t.Fatalf("account filter leaked other sources=%v", filtered)
	}
	account = ""
	empty, err := p.Items(ctx, &account)
	if err != nil || len(empty) != 1 || empty[0]["wishlisted"] != true {
		t.Fatalf("explicit empty account=%v err=%v", empty, err)
	}
	if _, err := p.Remove(ctx, []int64{220}); err != nil {
		t.Fatal(err)
	}
	remaining, err := p.Items(ctx, nil)
	if err != nil || len(remaining) != 1 || remaining[0]["appid"] != int64(620) {
		t.Fatalf("excluded still visible=%v err=%v", remaining, err)
	}
}
