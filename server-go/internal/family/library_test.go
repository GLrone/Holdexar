package family

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"holdexar/server-go/internal/wishlist"
)

func TestAchievementCandidatesReuseFamilySnapshot(t *testing.T) {
	s, db := fixture(t, Options{})
	bound(t, db, sidA)
	if _, err := db.Exec(`INSERT INTO family_groups(steamid,family_groupid,members_json,fetch_ok,member_count,updated_at)
		VALUES (?,'77',json_array(json_object('steamid',?),json_object('steamid',?)),1,2,CURRENT_TIMESTAMP)`, sidA, sidA, sidB); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO family_library_snapshots(appid,family_groupid,name,owners_json,time_acquired,presence,excluded,updated_at)
		VALUES (220,'77','Shared by Alpha',json_array(?),0,1,0,CURRENT_TIMESTAMP),
		       (221,'77','Already owned by guest',json_array(?),0,1,0,CURRENT_TIMESTAMP),
		       (222,'77','Not shareable',json_array(?),0,1,1,CURRENT_TIMESTAMP)`, sidA, sidB, sidA); err != nil {
		t.Fatal(err)
	}
	games, known, err := s.AchievementCandidates(context.Background(), sidB)
	if err != nil || !known || len(games) != 1 || games[220] != "Shared by Alpha" {
		t.Fatalf("unexpected guest candidates: %v, known=%v, err=%v", games, known, err)
	}
	games, known, err = s.AchievementCandidates(context.Background(), "76561198000000003")
	if err != nil || known || len(games) != 0 {
		t.Fatalf("non-member received candidates: %v, known=%v, err=%v", games, known, err)
	}
}

func TestLibraryPersistsOrderedOwnersAndPlayAcrossFailedRefresh(t *testing.T) {
	var fail atomic.Bool
	var block atomic.Bool
	entered, release := make(chan struct{}), make(chan struct{})
	var unblock sync.Once
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/group":
			fmt.Fprintf(w, `{"response":{"family_groupid":"77","family_group":{"name":"Family","members":[{"steamid":%q},{"steamid":%q}]}}}`, sidA, sidB)
		case "/shared":
			if block.Load() {
				close(entered)
				<-release
			}
			fmt.Fprintf(w, `{"response":{"apps":[{"appid":220,"name":"Shared","owner_steamids":[%q,%q],"rt_time_acquired":120,"presence_count":2,"exclude_reason":0}]}}`, sidB, sidA)
		case "/owned":
			sid := r.URL.Query().Get("steamid")
			if r.URL.Query().Get("access_token") != "token-"+sid {
				t.Error("member used another bound identity")
			}
			if fail.Load() && sid == sidB {
				w.WriteHeader(403)
				return
			}
			fmt.Fprint(w, `{"response":{"games":[{"appid":220,"name":"Shared","playtime_forever":42,"playtime_2weeks":12,"rtime_last_played":300}]}}`)
		default:
			w.WriteHeader(404)
		}
	}))
	defer upstream.Close()
	defer unblock.Do(func() { close(release) })
	lists := wishlist.NewSteam(wishlist.SteamOptions{HTTPClient: upstream.Client(), OwnedURL: upstream.URL + "/owned"})
	defer lists.Close()
	var kicked []int64
	s, db := fixture(t, Options{Lists: lists, GroupURL: upstream.URL + "/group", SharedURL: upstream.URL + "/shared", Token: func(_ context.Context, sid string) (string, error) { return "token-" + sid, nil }, OnUncatalogued: func(_ context.Context, ids []int64) error { kicked = ids; return nil }})
	bound(t, db, sidA)
	bound(t, db, sidB)
	ctx := context.Background()
	data, err := s.Library(ctx, sidA, false)
	if err != nil || len(data.Games) != 1 || data.Games[0].Buyer != sidA || data.Games[0].Playtime != 84 || data.FromSnapshot {
		t.Fatalf("live library %+v %v", data, err)
	}
	fail.Store(true)
	data, err = s.Library(ctx, sidA, true)
	if err != nil || !data.FromSnapshot || len(data.Play[sidB]) != 1 || data.Play[sidB][0].Minutes != 42 {
		t.Fatalf("refresh should return saved data %+v %v", data, err)
	}
	s.mu.Lock()
	flight := s.libraries[sidA].flight
	s.mu.Unlock()
	if flight != nil {
		<-flight.done
		if flight.err != nil {
			t.Fatal(flight.err)
		}
	}
	data, err = s.Library(ctx, sidA, false)
	if err != nil || data.FromSnapshot || len(data.Play[sidB]) != 1 || data.Play[sidB][0].Minutes != 42 {
		t.Fatalf("member history lost on successful partial refresh: %+v %v", data, err)
	}
	var snapshot, play string
	if err = db.QueryRow(`SELECT owners_json FROM family_library_snapshots WHERE family_groupid='77' AND appid=220`).Scan(&snapshot); err != nil || snapshot != packed([]string{sidB, sidA}) {
		t.Fatal(snapshot, err)
	}
	if err = db.QueryRow(`SELECT play_json FROM family_groups WHERE steamid=?`, sidA).Scan(&play); err != nil || play == "" {
		t.Fatal(play, err)
	}
	if _, err = db.Exec(`INSERT INTO wishlist_items(steamid,appid,active,owned,manual,wishlisted,manual_pool,excluded,board_pool) VALUES(?,220,1,0,0,1,0,0,0)`, sidA); err != nil {
		t.Fatal(err)
	}
	if _, err = s.Wishlist(ctx, sidA); err != nil || len(kicked) != 1 || kicked[0] != 220 {
		t.Fatalf("name-only row suppressed price discovery: %v %v", kicked, err)
	}
	block.Store(true)
	data, err = s.Library(ctx, sidA, true)
	if err != nil || !data.FromSnapshot {
		t.Fatalf("force waited for Steam: %+v %v", data, err)
	}
	select {
	case <-entered:
	case <-time.After(2 * time.Second):
		t.Fatal("refresh did not enter shared request")
	}
	s.mu.Lock()
	flight = s.libraries[sidA].flight
	s.mu.Unlock()
	if flight == nil {
		t.Fatal("expected in-progress refresh")
	}
	_, err = db.Exec(`UPDATE family_groups SET members_json=? WHERE steamid=?`, packed([]map[string]any{{"steamid": sidA, "origin": "steam"}}), sidA)
	if err != nil {
		t.Fatal(err)
	}
	unblock.Do(func() { close(release) })
	<-flight.done
	if flight.err != errIdentityChanged {
		t.Fatalf("old refresh survived membership change: %v", flight.err)
	}
	group, err := readGroup(ctx, db, sidA)
	if err != nil {
		t.Fatal(err)
	}
	data, err = s.librarySnapshot(ctx, group)
	if err != nil || len(data.Play[sidB]) != 0 || data.Games[0].Playtime != 42 {
		t.Fatalf("departed play returned from snapshot: %+v %v", data, err)
	}
}
