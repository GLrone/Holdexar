package family

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"holdexar/server-go/internal/wishlist"
)

func TestSyncKeepsPerAccountIdentityManualExclusionsAndFailureSnapshot(t *testing.T) {
	ctx := context.Background()
	var calls atomic.Int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		token := r.URL.Query().Get("access_token")
		if strings.Contains(r.URL.Path, "group") {
			calls.Add(1)
			if token == "token-"+sidB {
				w.WriteHeader(403)
				return
			}
			if token != "token-"+sidA {
				t.Error("credential owner changed")
			}
			fmt.Fprintf(w, `{"response":{"family_groupid":"g1","family_group":{"name":"Native family","members":[{"steamid":%q,"role":1},{"steamid":%q,"role":2},{"steamid":%q,"role":1}]}}}`, sidA, sidB, sidA)
			return
		}
		fmt.Fprint(w, `{"response":{"accounts":[]}}`)
	}))
	defer upstream.Close()
	lists := wishlist.NewSteam(wishlist.SteamOptions{HTTPClient: upstream.Client()})
	defer lists.Close()
	added := []string{}
	s, db := fixture(t, Options{Lists: lists, GroupURL: upstream.URL + "/group", LinkURL: upstream.URL + "/links", Token: func(_ context.Context, sid string) (string, error) { return "token-" + sid, nil }, Profile: func(_ context.Context, sid string) (map[string]any, error) {
		return map[string]any{"persona_name": "Name-" + sid, "avatar_url": "https://avatars.fastly.steamstatic.com/hash_full.jpg"}, nil
	}, OnMembers: func(ids []string) { added = append(added, ids...) }})
	bound(t, db, sidA)
	bound(t, db, sidB)
	if _, e := db.Exec(`INSERT INTO family_groups(steamid,family_groupid,family_name,members_json,fetch_ok,last_error,member_count,updated_at) VALUES(?,'old','Old group','[{"steamid":"76561198000000003","origin":"manual","personaName":"Manual"},{"steamid":"76561198000000002","origin":"steam","excluded":true}]',1,NULL,1,'2026-11-01'),(?,'keep','Keep group','[{"steamid":"76561198000000004","origin":"steam","personaName":"Keep"}]',1,NULL,1,'2026-11-01');INSERT INTO tracked_accounts(steamid,label,kinds_json,created_at) VALUES(?,'My note','{"wishlist":true,"owned":false}','2026-11-01')`, sidA, sidB, sidA); e != nil {
		t.Fatal(e)
	}
	v, e := s.Sync(ctx, "")
	if e != nil || v["synced"] != 2 || v["joined"] != 1 || v["failed"] != 1 || calls.Load() != 2 {
		t.Fatalf("isolated results %v %v", v, e)
	}
	state, e := s.Status(ctx)
	if e != nil {
		t.Fatal(e)
	}
	groups := state["groups"].([]map[string]any)
	if len(groups) != 2 || groups[1]["familyName"] != "Keep group" || groups[1]["lastError"] == nil || len(groups[1]["members"].([]map[string]any)) != 1 {
		t.Fatal(groups)
	}
	if len(groups[0]["members"].([]map[string]any)) != 2 {
		t.Fatal("excluded revived or manual lost", groups)
	}
	var count int
	db.QueryRow(`SELECT COUNT(*) FROM tracked_accounts WHERE steamid=?`, sidB).Scan(&count)
	if count != 0 {
		t.Fatal("excluded account automatically tracked")
	}
	var label, kinds string
	db.QueryRow(`SELECT label,kinds_json FROM tracked_accounts WHERE steamid=?`, sidA).Scan(&label, &kinds)
	if label != "My note" || !strings.Contains(kinds, `"owned":false`) || len(added) != 0 {
		t.Fatal(label, kinds, added)
	}
	if _, e = s.AddMember(ctx, sidB, "", "", sidA); e != nil {
		t.Fatal(e)
	}
	if _, e = s.Sync(ctx, sidA); e != nil {
		t.Fatal(e)
	}
	if len(added) != 1 || added[0] != sidB {
		t.Fatal("new member did not trigger exactly one automatic tracking sync", added)
	}
	g, e := readGroup(ctx, db, sidA)
	if e != nil || g.Count != 2 {
		t.Fatal("steam member count", g, e)
	}
}

func TestGroupAndTrackingCommitTogetherAndStaleResultCannotRecreateRemovedAccount(t *testing.T) {
	ctx := context.Background()
	entered, release := make(chan struct{}), make(chan struct{})
	var block atomic.Bool
	var once sync.Once
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/group" {
			if block.Load() {
				once.Do(func() { close(entered) })
				select {
				case <-release:
				case <-r.Context().Done():
					return
				}
			}
			fmt.Fprintf(w, `{"response":{"family_groupid":"g","family_group":{"members":[{"steamid":%q,"role":"1"}]}}}`, sidA)
		} else {
			fmt.Fprint(w, `{"response":{"accounts":[]}}`)
		}
	}))
	defer upstream.Close()
	lists := wishlist.NewSteam(wishlist.SteamOptions{HTTPClient: upstream.Client()})
	defer lists.Close()
	s, db := fixture(t, Options{Lists: lists, GroupURL: upstream.URL + "/group", LinkURL: upstream.URL + "/links", Token: func(context.Context, string) (string, error) { return "token", nil }})
	bound(t, db, sidA)
	if _, e := db.Exec(`CREATE TRIGGER reject_family BEFORE INSERT ON family_groups BEGIN SELECT RAISE(ABORT,'refuse family'); END`); e != nil {
		t.Fatal(e)
	}
	if _, e := s.Sync(ctx, sidA); e == nil {
		t.Fatal("group failure accepted")
	}
	var n int
	db.QueryRow(`SELECT COUNT(*) FROM tracked_accounts`).Scan(&n)
	if n != 0 {
		t.Fatal("tracking committed before group", n)
	}
	if _, e := db.Exec(`DROP TRIGGER reject_family`); e != nil {
		t.Fatal(e)
	}
	block.Store(true)
	done := make(chan error, 1)
	go func() { _, e := s.Sync(ctx, sidA); done <- e }()
	select {
	case <-entered:
	case <-time.After(time.Second):
		close(release)
		t.Fatal("request absent")
	}
	if _, e := db.Exec(`DELETE FROM steam_accounts WHERE steam_id=?`, sidA); e != nil {
		close(release)
		t.Fatal(e)
	}
	close(release)
	if e := <-done; e == nil {
		t.Fatal("stale request succeeded")
	}
	db.QueryRow(`SELECT COUNT(*) FROM family_groups`).Scan(&n)
	if n != 0 {
		t.Fatal("stale group resurrected", n)
	}
}

func TestProfileHealingRechecksConcurrentExclusionAndNickname(t *testing.T) {
	ctx := context.Background()
	entered, release := make(chan struct{}), make(chan struct{})
	var once sync.Once
	s, db := fixture(t, Options{Profile: func(ctx context.Context, sid string) (map[string]any, error) {
		once.Do(func() { close(entered) })
		select {
		case <-release:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
		return map[string]any{"persona_name": "Remote old", "avatar_url": "https://avatars.fastly.steamstatic.com/hash_full.jpg"}, nil
	}})
	bound(t, db, sidA)
	if _, e := db.Exec(`INSERT INTO family_groups(steamid,members_json,fetch_ok,member_count,updated_at) VALUES(?,'[{"steamid":"76561198000000002","origin":"steam","personaName":"","avatarUrl":""}]',1,1,'2026-11-01')`, sidA); e != nil {
		t.Fatal(e)
	}
	done := make(chan error, 1)
	go func() { _, e := s.Status(ctx); done <- e }()
	select {
	case <-entered:
	case <-time.After(time.Second):
		close(release)
		t.Fatal("no profile")
	}
	if _, e := db.Exec(`UPDATE family_groups SET members_json='[{"steamid":"76561198000000002","origin":"steam","personaName":"User new","excluded":true}]' WHERE steamid=?`, sidA); e != nil {
		close(release)
		t.Fatal(e)
	}
	close(release)
	if e := <-done; e != nil {
		t.Fatal(e)
	}
	g, e := readGroup(ctx, db, sidA)
	if e != nil || !yes(g.Members[0]["excluded"]) || memberName(g.Members[0]) != "User new" {
		t.Fatal(g, e)
	}
}
