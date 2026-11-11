package wishlist

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"holdexar/server-go/internal/monitoring"
)

func TestAccountSyncMergesWishAndOwnedPreservesOthersAndExclusions(t *testing.T) {
	pool, db := poolFixture(t)
	ctx := context.Background()
	_, e := db.Exec(`INSERT INTO tracked_accounts(steamid,kinds_json,created_at) VALUES('a','{"wishlist":true,"owned":true}','2026-11-01'),('b','{"wishlist":true,"owned":true}','2026-11-01');
 INSERT INTO wishlist_items VALUES('a',300,NULL,0,0,0,1,0,1,0),('a',600,NULL,1,0,0,1,0,0,0),('a',700,NULL,1,1,0,0,0,0,0),('b',500,NULL,1,1,0,0,0,0,0);`)
	if e != nil {
		t.Fatal(e)
	}
	on := true
	if _, e = pool.Monitor.Apply(ctx, "game", []int64{300}, monitoring.Change{Excluded: &on, Reason: "user"}); e != nil {
		t.Fatal(e)
	}
	if _, e = pool.Monitor.Apply(ctx, "game", []int64{600, 700, 500}, monitoring.Change{AddOnly: true, Sources: map[int64][]string{600: {"family_wishlist", "favorite"}, 700: {"owned"}, 500: {"owned"}}}); e != nil {
		t.Fatal(e)
	}
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sid := r.URL.Query().Get("steamid")
		if r.URL.Path == "/wish" {
			if sid == "a" {
				fmt.Fprint(w, `{"response":{"items":[{"appid":100},{"appid":200},{"appid":300}]}}`)
			} else {
				fmt.Fprint(w, `{"response":{"items":[{"appid":200}]}}`)
			}
			return
		}
		if r.URL.Query().Get("access_token") != "token-"+sid {
			t.Error("target credential mixed")
		}
		if sid == "b" {
			w.WriteHeader(401)
			return
		}
		fmt.Fprint(w, `{"response":{"games":[{"appid":200},{"appid":400}]}}`)
	}))
	defer upstream.Close()
	steam := NewSteam(SteamOptions{HTTPClient: upstream.Client(), WishlistURL: upstream.URL + "/wish", OwnedURL: upstream.URL + "/owned", Token: func(_ context.Context, sid string) (string, error) { return "token-" + sid, nil }})
	s := NewService(ctx, pool, ServiceOptions{Steam: steam})
	t.Cleanup(func() { s.Close(context.Background()) })
	result, e := s.SyncAccount(ctx, "a", false)
	if e != nil || result["added"] != 3 || result["addedOwned"] != 2 || result["active"] != 3 {
		t.Fatalf("a=%v error=%v", result, e)
	}
	result, e = s.SyncAccount(ctx, "b", false)
	if e != nil || result["ownedError"] == nil || result["active"] != 2 {
		t.Fatalf("b=%v error=%v", result, e)
	}
	var active, wished, owned bool
	if e = db.QueryRow(`SELECT active,wishlisted,owned FROM wishlist_items WHERE steamid='a' AND appid=200`).Scan(&active, &wished, &owned); e != nil || !active || !wished || !owned {
		t.Fatalf("intersection=%t/%t/%t %v", active, wished, owned, e)
	}
	if e = db.QueryRow(`SELECT active,owned FROM wishlist_items WHERE steamid='b' AND appid=500`).Scan(&active, &owned); e != nil || !active || !owned {
		t.Fatal("owned failure erased old account facts", e)
	}
	if e = db.QueryRow(`SELECT active FROM wishlist_items WHERE steamid='a' AND appid=300`).Scan(&active); e != nil || active {
		t.Fatal("excluded account row revived", e)
	}
	for id, want := range map[int64]string{300: "excluded", 600: "active", 700: "released", 200: "active"} {
		state, e := pool.Monitor.Snapshot(ctx, "game", id)
		if e != nil || state["state"] != want {
			t.Fatalf("id=%d state=%v error=%v", id, state, e)
		}
	}
}

func TestSyncDiscardedAfterUntrackAndReaddAndDuplicateIsCoalesced(t *testing.T) {
	pool, db := poolFixture(t)
	ctx := context.Background()
	if _, e := db.Exec(`INSERT INTO tracked_accounts(steamid,kinds_json,created_at) VALUES('a','{"wishlist":true,"owned":false}','2026-11-01')`); e != nil {
		t.Fatal(e)
	}
	entered, release := make(chan struct{}), make(chan struct{})
	var calls atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		close(entered)
		<-release
		json.NewEncoder(w).Encode(map[string]any{"response": map[string]any{"items": []any{map[string]int{"appid": 620}}}})
	}))
	t.Cleanup(upstream.Close)
	steam := NewSteam(SteamOptions{HTTPClient: upstream.Client(), WishlistURL: upstream.URL})
	s := NewService(ctx, pool, ServiceOptions{Steam: steam})
	t.Cleanup(func() { s.Close(context.Background()) })
	done := make(chan error, 1)
	go func() { _, e := s.SyncAccount(ctx, "a", false); done <- e }()
	select {
	case <-entered:
	case <-time.After(3 * time.Second):
		t.Fatal("sync never fetched")
	}
	result, e := s.SyncAccount(ctx, "a", false)
	if e != nil || result["inflight"] != true || calls.Load() != 1 {
		t.Fatalf("duplicate=%v %v", result, e)
	}
	if _, e = db.Exec(`DELETE FROM tracked_accounts WHERE steamid='a'; INSERT INTO tracked_accounts(steamid,kinds_json,created_at) VALUES('a','{"wishlist":true,"owned":false}','2026-11-02')`); e != nil {
		t.Fatal(e)
	}
	close(release)
	if e = <-done; e == nil {
		t.Fatal("old sync wrote into newly registered target")
	}
	var n int
	if e = db.QueryRow(`SELECT count(*) FROM wishlist_items`).Scan(&n); e != nil || n != 0 {
		t.Fatalf("old facts revived count=%d %v", n, e)
	}
}
