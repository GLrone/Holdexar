package wishlist

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"holdexar/server-go/internal/crawl"
	"holdexar/server-go/internal/jobrun"
	"holdexar/server-go/internal/monitoring"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/storebrowse"
)

func TestBusyFirstCrawlContinuesOnIdleAndFillsMissingGlobalRegion(t *testing.T) {
	pool, db := poolFixture(t)
	ctx := context.Background()
	if _, e := db.Exec(`UPDATE crawl_regions SET enabled=(code='cn'); INSERT INTO tracked_accounts(steamid,kinds_json,created_at) VALUES('a','{"wishlist":true,"owned":false}','2026-11-01'),('b','{"wishlist":true,"owned":false}','2026-11-02'); INSERT INTO app_settings(key,value_json) VALUES('crawl.owned_regions','["us"]') ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json; INSERT INTO game_current_prices(appid,region_code,currency,price,cny_fen,original_price,discount_percent,price_status,fail_count) VALUES(221,'US','USD',10,70,10,0,'ok',0)`); e != nil {
		t.Fatal(e)
	}
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := 220
		if r.URL.Query().Get("steamid") == "b" {
			id = 221
		}
		fmt.Fprintf(w, `{"response":{"items":[{"appid":%d}]}}`, id)
	}))
	defer upstream.Close()
	entered, release := make(chan struct{}), make(chan struct{})
	var block, unblock sync.Once
	var s *Service
	runtime := crawl.New(db, pool.Gate, nil, crawl.Options{AfterIdle: func(stopped bool) { s.PricesIdle(stopped) }, Fetch: func(ctx context.Context, ids []int64, cc, lang string, extras bool) (map[int64]storebrowse.Item, error) {
		if len(ids) > 0 && ids[0] == 220 {
			block.Do(func() { close(entered) })
			select {
			case <-release:
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}
		items := map[int64]storebrowse.Item{}
		for _, id := range ids {
			items[id] = storebrowse.Item{"appid": id, "success": 1, "visible": true, "type": 0, "name": "fixture", "purchase_options": []any{map[string]any{"packageid": "39", "final_price_in_cents": "999", "original_price_in_cents": "1999", "discount_pct": "50"}}}
		}
		return items, nil
	}})
	s = NewService(ctx, pool, ServiceOptions{Steam: NewSteam(SteamOptions{HTTPClient: upstream.Client(), WishlistURL: upstream.URL}), Crawl: func(_ context.Context, w, o []int64) (bool, error) { return runtime.StartAccountDiscovery(ctx, w, o) }, CrawlBusy: func(e error) bool { return errors.Is(e, crawl.ErrBusy) }})
	t.Cleanup(func() { unblock.Do(func() { close(release) }); s.Close(ctx); runtime.Close(ctx) })
	if _, e := s.SyncAccount(ctx, "a", true); e != nil {
		t.Fatal(e)
	}
	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("first crawl absent")
	}
	b, e := s.SyncAccount(ctx, "b", true)
	if e != nil || b["crawlTriggered"] != false {
		t.Fatalf("busy not observed %v %v", b, e)
	}
	unblock.Do(func() { close(release) })
	deadline := time.Now().Add(4 * time.Second)
	for {
		var n int
		db.QueryRow(`SELECT COUNT(*) FROM game_current_prices WHERE appid=221 AND region_code='CN'`).Scan(&n)
		if n == 1 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("busy handoff lost or US price hid missing CN")
		}
		time.Sleep(time.Millisecond)
	}
	if e = s.Close(ctx); e != nil {
		t.Fatal(e)
	}
	if e = runtime.Close(ctx); e != nil {
		t.Fatal(e)
	}
	var jobs int
	db.QueryRow(`SELECT COUNT(*) FROM crawl_jobs WHERE kind='wishlist_sync'`).Scan(&jobs)
	if jobs != 2 {
		t.Fatalf("handoff duplicated jobs %d", jobs)
	}
}

func TestPriceIdleDuringSubmissionDoesNotLoseWakeupOrRepeatFailedPrice(t *testing.T) {
	pool, db := poolFixture(t)
	ctx := context.Background()
	_, e := db.Exec(`UPDATE crawl_regions SET enabled=(code='cn');INSERT INTO tracked_accounts(steamid,created_at) VALUES('a','2026-11-01');INSERT INTO wishlist_items(steamid,appid,active,owned,wishlisted,manual,excluded,manual_pool,board_pool) VALUES('a',220,1,0,1,0,0,0,0)`)
	if e != nil {
		t.Fatal(e)
	}
	if _, e = pool.Monitor.Apply(ctx, "game", []int64{220}, monitoring.Change{AddOnly: true, Sources: map[int64][]string{220: {"family_wishlist"}}}); e != nil {
		t.Fatal(e)
	}
	var calls atomic.Int32
	var s *Service
	s = NewService(ctx, pool, ServiceOptions{CrawlBusy: func(e error) bool { return errors.Is(e, crawl.ErrBusy) }, Crawl: func(context.Context, []int64, []int64) (bool, error) {
		n := calls.Add(1)
		s.PricesIdle(false)
		if n == 1 {
			return false, crawl.ErrBusy
		}
		return true, nil
	}})
	t.Cleanup(func() { s.Close(ctx) })
	if _, e = s.startPrices(ctx, []int64{220}, nil); e != crawl.ErrBusy {
		t.Fatal(e)
	}
	deadline := time.Now().Add(time.Second)
	for calls.Load() < 2 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	time.Sleep(25 * time.Millisecond)
	s.mu.Lock()
	pending := s.pricePending
	s.mu.Unlock()
	if calls.Load() != 2 || pending != 0 {
		t.Fatalf("lost wakeup or idle failure loop calls=%d pending=%d", calls.Load(), pending)
	}
	s.mu.Lock()
	s.pricePending = 3
	s.mu.Unlock()
	s.PricesIdle(true)
	s.mu.Lock()
	pending = s.pricePending
	s.mu.Unlock()
	if pending != 0 {
		t.Fatal("user stop retained continuation")
	}
}

func TestInvalidOwnedRegionPlanningDoesNotLoopOnIdle(t *testing.T) {
	pool, db := poolFixture(t)
	ctx := context.Background()
	if _, e := db.Exec(`UPDATE crawl_regions SET enabled=(code='cn'); INSERT INTO tracked_accounts(steamid,created_at) VALUES('a','2026-11-01'); INSERT INTO wishlist_items(steamid,appid,active,owned,wishlisted,manual,excluded,manual_pool,board_pool) VALUES('a',220,1,1,0,0,0,0,0); INSERT INTO app_settings(key,value_json) VALUES('crawl.owned_regions','["cn","unknown"]') ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json`); e != nil {
		t.Fatal(e)
	}
	if _, e := pool.Monitor.Apply(ctx, "game", []int64{220}, monitoring.Change{AddOnly: true, Sources: map[int64][]string{220: {"owned"}}}); e != nil {
		t.Fatal(e)
	}
	var s *Service
	var calls atomic.Int32
	runtime := crawl.New(db, pool.Gate, nil, crawl.Options{AfterIdle: func(stopped bool) { s.PricesIdle(stopped) }})
	s = NewService(ctx, pool, ServiceOptions{CrawlBusy: func(e error) bool { return errors.Is(e, crawl.ErrBusy) }, Crawl: func(_ context.Context, w, o []int64) (bool, error) {
		calls.Add(1)
		return runtime.StartAccountDiscovery(ctx, w, o)
	}})
	t.Cleanup(func() { s.Close(ctx); runtime.Close(ctx) })
	if _, e := s.Backfill(ctx); e == nil {
		t.Fatal("invalid configuration accepted")
	}
	time.Sleep(40 * time.Millisecond)
	if e := s.Close(ctx); e != nil {
		t.Fatal(e)
	}
	s.mu.Lock()
	pending := s.pricePending
	s.mu.Unlock()
	if calls.Load() > 2 || pending != 0 {
		t.Fatalf("planning failure idle loop: %d, pending=%d", calls.Load(), pending)
	}
}

func TestTrackingHTTPImmediateSyncLibraryOwnershipAndRemoval(t *testing.T) {
	pool, db := poolFixture(t)
	ctx := context.Background()
	const a = "76561198000000001"
	const b = "76561198000000002"
	var ownCalls atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/wish" {
			fmt.Fprint(w, `{"response":{"items":[{"appid":220},{"appid":221}]}}`)
			return
		}
		ownCalls.Add(1)
		fmt.Fprint(w, `{"response":{"games":[{"appid":220},{"appid":222}]}}`)
	}))
	defer upstream.Close()
	steam := NewSteam(SteamOptions{HTTPClient: upstream.Client(), WishlistURL: upstream.URL + "/wish", OwnedURL: upstream.URL + "/owned", Token: func(context.Context, string) (string, error) { return "scoped", nil }})
	s := NewService(ctx, pool, ServiceOptions{Steam: steam})
	t.Cleanup(func() { s.Close(ctx) })
	mux := http.NewServeMux()
	s.Register(mux)
	pool.Register(mux)
	call := func(method, path, body string) (int, any) {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		w := httptest.NewRecorder()
		mux.ServeHTTP(w, r)
		var out any
		if e := json.Unmarshal(w.Body.Bytes(), &out); e != nil {
			t.Fatal(w.Body.String())
		}
		return w.Code, out
	}
	code, v := call("POST", "/api/v1/accounts", `{"steamid":"`+a+`","label":"A"}`)
	if code != 200 {
		t.Fatalf("add %d %v", code, v)
	}
	deadline := time.Now().Add(3 * time.Second)
	for {
		var n int
		db.QueryRow(`SELECT COUNT(*) FROM wishlist_items WHERE steamid=?`, a).Scan(&n)
		if n == 3 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("automatic bind sync absent: %d", n)
		}
		time.Sleep(time.Millisecond)
	}
	if code, _ = call("POST", "/api/v1/accounts", `{"steamid":"`+a+`"}`); code != 400 {
		t.Fatalf("duplicate %d", code)
	}
	if code, _ = call("PUT", "/api/v1/accounts/absent/kinds", `{"owned":false}`); code != 400 {
		t.Fatalf("missing kinds %d", code)
	}
	if code, _ = call("POST", "/api/v1/accounts/absent/sync", ``); code != 400 {
		t.Fatalf("missing sync %d", code)
	}
	if code, _ = call("POST", "/api/v1/accounts", `{"steamid":"`+a+`"} {}`); code != 422 {
		t.Fatalf("trailing JSON %d", code)
	}
	if code, _ = call("GET", "/api/v1/ownership?appids=abc", ``); code != 400 {
		t.Fatalf("invalid IDs %d", code)
	}
	if code, _ = call("GET", "/api/v1/ownership", ``); code != 422 {
		t.Fatalf("missing IDs %d", code)
	}
	_, e := db.Exec(`INSERT INTO tracked_accounts(steamid,label,kinds_json,created_at) VALUES(?,'B','{"wishlist":true,"owned":false}','2026-01-01'); INSERT INTO wishlist_items(steamid,appid,active,owned,wishlisted,manual,excluded,manual_pool,board_pool) VALUES(?,220,1,1,0,0,0,0,0),(?,223,1,1,0,0,0,0,0),(?,224,1,0,0,0,0,0,0);`, b, b, b, b)
	if e != nil {
		t.Fatal(e)
	}
	_, e = db.Exec(`INSERT INTO steam_accounts(steam_id,cookies,persona_name,avatar_url,is_active,is_online,in_game,bound_at,wallet_error,wallet_backoff_level,wallet_fail_streak,wallet_frozen) VALUES(?,'','','',0,0,'','2026-01-01','',0,0,0),(?,'','','',1,0,'','2026-02-01','',0,0,0);`, a, b)
	if e != nil {
		t.Fatal(e)
	}
	_, e = db.Exec(`INSERT INTO game_current_prices(appid,region_code,currency,price,cny_fen,original_price,discount_percent,price_status,fail_count) VALUES(220,'CN','CNY',1234,1234,2468,50,'ok',0),(222,'CN','CNY',0,0,0,0,'ok',0); INSERT INTO tags(tagid,name_zh,name_en) VALUES(1,'动作','Action'); INSERT INTO game_tags(appid,tagid,weight) VALUES(220,1,10);`)
	if e != nil {
		t.Fatal(e)
	}
	code, v = call("GET", "/api/v1/owned-library", ``)
	if code != 200 {
		t.Fatalf("library %d %v", code, v)
	}
	lib := v.(map[string]any)
	accounts := lib["accounts"].([]any)
	first := accounts[0].(map[string]any)
	if first["steamid"] != a || first["isPrimary"] != true || first["ownedCount"] != float64(2) || first["valueFen"] != float64(1234) || first["freeCount"] != float64(1) {
		t.Fatalf("primary totals %v", accounts)
	}
	games := lib["games"].([]any)
	if len(games) != 3 || len(games[0].(map[string]any)["owners"].([]any)) != 2 || len(games[0].(map[string]any)["tags"].([]any)) != 1 || games[2].(map[string]any)["cnPriceFen"] != nil {
		t.Fatalf("library actual %v", lib)
	}
	code, v = call("GET", "/api/v1/ownership?appids=220,221,223,224", ``)
	if code != 200 {
		t.Fatal(code, v)
	}
	facts := v.(map[string]any)["ownerships"].(map[string]any)
	if facts["220"].(map[string]any)["type"] != "owned" || facts["221"].(map[string]any)["type"] != "wishlist" || facts["223"].(map[string]any)["type"] != "family" || facts["224"] != nil {
		t.Fatal(facts)
	}
	code, _ = call("PUT", "/api/v1/accounts/"+a+"/kinds", `{"owned":false}`)
	if code != 200 {
		t.Fatal(code)
	}
	code, v = call("POST", "/api/v1/accounts/"+a+"/sync?autoCrawl=false", ``)
	if code != 200 || ownCalls.Load() != 1 {
		t.Fatalf("disabled still fetched %d %v %d", code, v, ownCalls.Load())
	}
	code, v = call("DELETE", "/api/v1/accounts/"+a, ``)
	if code != 200 {
		t.Fatal(code, v)
	}
	var bound int
	db.QueryRow(`SELECT COUNT(*) FROM steam_accounts WHERE steam_id=?`, a).Scan(&bound)
	if bound != 1 {
		t.Fatal("tracked unlink deleted bound credential")
	}
	if code, _ = call("DELETE", "/api/v1/accounts/"+a, ``); code != 404 {
		t.Fatal("missing remove", code)
	}
}

func TestWishlistScheduleRunsWithoutPriceAutoAndBackfillUsesMonitorFacts(t *testing.T) {
	pool, db := poolFixture(t)
	ctx := context.Background()
	var calls atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		fmt.Fprint(w, `{"response":{"items":[{"appid":220}]}}`)
	}))
	defer upstream.Close()
	now := time.Date(2026, 11, 7, 12, 0, 0, 0, wishlistZone)
	var wished, owned []int64
	var crawlCalls int
	s := NewService(ctx, pool, ServiceOptions{Steam: NewSteam(SteamOptions{HTTPClient: upstream.Client(), WishlistURL: upstream.URL}), Now: func() time.Time { return now }, Recorder: jobrun.New(db, pool.Gate, func() time.Time { return now }, func(string, ...any) {}), Crawl: func(_ context.Context, w, o []int64) (bool, error) {
		crawlCalls++
		wished = w
		owned = o
		return true, nil
	}})
	t.Cleanup(func() { s.Close(ctx) })
	if _, e := db.Exec(`INSERT INTO tracked_accounts(steamid,kinds_json,created_at) VALUES('a','{"wishlist":true,"owned":false}','2026-11-01'); INSERT INTO app_settings(key,value_json) VALUES('crawl.auto_price','false') ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json;`); e != nil {
		t.Fatal(e)
	}
	clock := sched.NewScheduler()
	if e := s.RegisterSchedules(ctx, clock); e != nil {
		t.Fatal(e)
	}
	clock.StartSim(now)
	clock.RunSim(now.Add(29 * time.Second))
	if calls.Load() != 0 {
		t.Fatal("premature startup")
	}
	clock.RunSim(now.Add(30 * time.Second))
	if calls.Load() != 1 || crawlCalls != 0 {
		t.Fatalf("auto false lost data sync: %d %d", calls.Load(), crawlCalls)
	}
	var runs int
	if e := db.QueryRow(`SELECT COUNT(*) FROM agent_runs WHERE runner='job' AND json_extract(meta,'$.job')='wishlist_sync' AND status='done'`).Scan(&runs); e != nil || runs != 1 {
		t.Fatalf("task record %d %v", runs, e)
	}
	if _, e := db.Exec(`UPDATE app_settings SET value_json='true' WHERE key='crawl.auto_price'; INSERT INTO wishlist_items(steamid,appid,active,owned,wishlisted,manual,excluded,manual_pool,board_pool) VALUES('a',221,1,1,0,0,0,0,0),('a',222,1,1,0,0,0,0,0);`); e != nil {
		t.Fatal(e)
	}
	on := true
	if _, e := pool.Monitor.Apply(ctx, "game", []int64{221, 222}, monitoring.Change{AddOnly: true, Sources: map[int64][]string{221: {"owned"}, 222: {"owned"}}}); e != nil {
		t.Fatal(e)
	}
	if _, e := pool.Monitor.Apply(ctx, "game", []int64{222}, monitoring.Change{Excluded: &on}); e != nil {
		t.Fatal(e)
	}
	if _, e := s.Backfill(ctx); e != nil {
		t.Fatal(e)
	}
	if fmt.Sprint(wished) != "[220]" || fmt.Sprint(owned) != "[221]" {
		t.Fatalf("backfill bypassed exclusions %v %v", wished, owned)
	}
	clock.RunSim(now.Add(15*time.Minute + 30*time.Second))
	if calls.Load() != 2 {
		t.Fatal("15min sync absent", calls.Load())
	}
	clock.Stop()
}

func TestBackgroundSyncRespectsAutoPrice(t *testing.T) {
	for _, enabled := range []bool{false, true} {
		t.Run(fmt.Sprint(enabled), func(t *testing.T) {
			pool, db := poolFixture(t)
			ctx := context.Background()
			if _, e := db.Exec(`INSERT INTO tracked_accounts(steamid,kinds_json,created_at) VALUES('a','{"wishlist":true,"owned":false}','2026-11-01'); INSERT INTO app_settings(key,value_json) VALUES('crawl.auto_price',?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json`, fmt.Sprint(enabled)); e != nil {
				t.Fatal(e)
			}
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				fmt.Fprint(w, `{"response":{"items":[{"appid":220}]}}`)
			}))
			defer upstream.Close()
			var crawled bool
			s := NewService(ctx, pool, ServiceOptions{Steam: NewSteam(SteamOptions{HTTPClient: upstream.Client(), WishlistURL: upstream.URL}), Crawl: func(context.Context, []int64, []int64) (bool, error) {
				crawled = true
				return true, nil
			}})
			t.Cleanup(func() { s.Close(ctx) })
			if !s.RequestSync("a", true) {
				t.Fatal("sync rejected")
			}
			s.jobs.Wait()
			var count int
			if e := db.QueryRow(`SELECT COUNT(*) FROM wishlist_items WHERE steamid='a' AND appid=220 AND wishlisted=1`).Scan(&count); e != nil || count != 1 {
				t.Fatalf("wishlist not saved: %d %v", count, e)
			}
			if crawled != enabled {
				t.Fatalf("price request = %v, want %v", crawled, enabled)
			}
		})
	}
}

func TestPendingSyncSurvivesRemoveAndReaddAndClose(t *testing.T) {
	pool, db := poolFixture(t)
	ctx := context.Background()
	var calls atomic.Int32
	entered, release := make(chan struct{}), make(chan struct{})
	var once sync.Once
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := calls.Add(1)
		if n == 1 {
			close(entered)
			select {
			case <-release:
			case <-r.Context().Done():
				return
			}
		}
		fmt.Fprintf(w, `{"response":{"items":[{"appid":%d}]}}`, 600+n)
	}))
	defer upstream.Close()
	t.Cleanup(func() { once.Do(func() { close(release) }) })
	_, e := db.Exec(`INSERT INTO tracked_accounts(steamid,kinds_json,created_at) VALUES('a','{"wishlist":true,"owned":false}','2026-11-01')`)
	if e != nil {
		t.Fatal(e)
	}
	s := NewService(ctx, pool, ServiceOptions{Steam: NewSteam(SteamOptions{HTTPClient: upstream.Client(), WishlistURL: upstream.URL})})
	t.Cleanup(func() { once.Do(func() { close(release) }); s.Close(ctx) })
	if !s.RequestSync("a", false) {
		t.Fatal("start rejected")
	}
	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("no request")
	}
	_, e = db.Exec(`DELETE FROM tracked_accounts WHERE steamid='a'; INSERT INTO tracked_accounts(steamid,kinds_json,created_at) VALUES('a','{"wishlist":true,"owned":false}','2026-11-02')`)
	if e != nil {
		t.Fatal(e)
	}
	for i := 0; i < 10; i++ {
		if !s.RequestSync("a", false) {
			t.Fatal("pending rejected")
		}
	}
	once.Do(func() { close(release) })
	deadline := time.Now().Add(3 * time.Second)
	for {
		var n int
		db.QueryRow(`SELECT COUNT(*) FROM wishlist_items WHERE steamid='a' AND appid=602`).Scan(&n)
		if n == 1 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("rebind request lost calls=%d", calls.Load())
		}
		time.Sleep(time.Millisecond)
	}
	if e = s.Close(ctx); e != nil {
		t.Fatal(e)
	}
	if calls.Load() != 2 || s.RequestSync("a", false) {
		t.Fatalf("coalescing/close %d", calls.Load())
	}
}
