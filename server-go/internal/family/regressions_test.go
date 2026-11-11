package family

import (
	"context"
	"database/sql"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"

	"holdexar/server-go/internal/wishlist"
)

func TestProjectedLocalMemberRemovableBeforeGroupSync(t *testing.T) {
	s, db := fixture(t, Options{})
	ctx := context.Background()
	if _, e := s.AddMember(ctx, sidB, "Manual", "", ""); e != nil {
		t.Fatal(e)
	}
	bound(t, db, sidA)
	receipt, e := s.RemoveMember(ctx, sidB, sidA)
	if e != nil || receipt["removed"] != true {
		t.Fatal(receipt, e)
	}
	state, e := s.Status(ctx)
	if e != nil || len(flat(t, state)) != 0 {
		t.Fatal(state, e)
	}
}
func TestOneRemovalClearsOverlappingManualAndSteamMembership(t *testing.T) {
	ctx := context.Background()
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/group" {
			fmt.Fprintf(w, `{"response":{"family_groupid":"g","family_group":{"members":[{"steamid":%q,"role":1}]}}}`, sidB)
		} else {
			fmt.Fprint(w, `{"response":{"accounts":[]}}`)
		}
	}))
	defer up.Close()
	lists := wishlist.NewSteam(wishlist.SteamOptions{HTTPClient: up.Client()})
	defer lists.Close()
	s, db := fixture(t, Options{Lists: lists, GroupURL: up.URL + "/group", LinkURL: up.URL + "/links", Token: func(context.Context, string) (string, error) { return "token", nil }})
	bound(t, db, sidA)
	if _, e := s.AddMember(ctx, sidB, "Manual", "", sidA); e != nil {
		t.Fatal(e)
	}
	if _, e := s.Sync(ctx, sidA); e != nil {
		t.Fatal(e)
	}
	if _, e := s.RemoveMember(ctx, sidB, sidA); e != nil {
		t.Fatal(e)
	}
	state, e := s.Status(ctx)
	if e != nil || len(flat(t, state)) != 0 {
		t.Fatalf("overlapping member still visible %v %v", state, e)
	}
	if _, e := s.Sync(ctx, sidA); e != nil {
		t.Fatal(e)
	}
	state, e = s.Status(ctx)
	if e != nil || len(flat(t, state)) != 0 {
		t.Fatalf("removed member resurrected %v %v", state, e)
	}
}
func TestCredentialChangedWhileAcquiringTokenUsesFreshIdentity(t *testing.T) {
	ctx := context.Background()
	seen := make(chan string, 2)
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen <- r.URL.Query().Get("access_token")
		fmt.Fprint(w, `{"response":{}}`)
	}))
	defer up.Close()
	lists := wishlist.NewSteam(wishlist.SteamOptions{HTTPClient: up.Client()})
	defer lists.Close()
	var db *sql.DB
	attempts := 0
	s, db := fixture(t, Options{Lists: lists, GroupURL: up.URL, Token: func(_ context.Context, sid string) (string, error) {
		attempts++
		if attempts == 1 {
			_, e := db.Exec(`UPDATE steam_accounts SET cookies='new-version' WHERE steam_id=?`, sid)
			return "old-token", e
		}
		return "new-token", nil
	}})
	bound(t, db, sidA)
	if _, e := s.Sync(ctx, sidA); e != nil {
		t.Fatal(e)
	}
	select {
	case token := <-seen:
		if token != "new-token" || attempts != 2 {
			t.Fatal("stale token dispatched after rebinding", attempts)
		}
	default:
		t.Fatal("no family request")
	}
}

func TestPrimaryRemovalAlsoClearsProjectedLocalDuplicateButSecondaryDoesNot(t *testing.T) {
	s, db := fixture(t, Options{})
	ctx := context.Background()
	if _, e := s.AddMember(ctx, sidB, "Before binding", "", ""); e != nil {
		t.Fatal(e)
	}
	bound(t, db, sidA)
	bound(t, db, sidB)
	if _, e := db.Exec(`INSERT INTO family_groups(steamid,members_json,fetch_ok,member_count,updated_at) SELECT steam_id,'[{"steamid":"76561198000000002","origin":"steam"}]',1,1,'2026-11-07' FROM steam_accounts`); e != nil {
		t.Fatal(e)
	}
	if _, e := s.RemoveMember(ctx, sidB, sidB); e != nil {
		t.Fatal(e)
	}
	local, e := readGroup(ctx, db, localGroup)
	if e != nil || len(local.Members) != 1 {
		t.Fatal("secondary erased unrelated local member", local, e)
	}
	if _, e = db.Exec(`CREATE TRIGGER reject_primary_removal BEFORE UPDATE ON family_groups WHEN NEW.steamid='76561198000000001' BEGIN SELECT RAISE(ABORT,'reject primary'); END`); e != nil {
		t.Fatal(e)
	}
	if _, e = s.RemoveMember(ctx, sidB, sidA); e == nil {
		t.Fatal("injected primary failure accepted")
	}
	local, e = readGroup(ctx, db, localGroup)
	if e != nil || len(local.Members) != 1 {
		t.Fatal("primary failure erased local member", local, e)
	}
	if _, e = db.Exec(`DROP TRIGGER reject_primary_removal`); e != nil {
		t.Fatal(e)
	}
	if _, e = s.RemoveMember(ctx, sidB, sidA); e != nil {
		t.Fatal(e)
	}
	st, e := s.Status(ctx)
	if e != nil || len(flat(t, st)) != 0 {
		t.Fatalf("local duplicate resurfaced %v %v", st, e)
	}
}

func TestOneGroupWriteFailureDoesNotStopLaterAccounts(t *testing.T) {
	ctx := context.Background()
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, `{"response":{}}`) }))
	defer up.Close()
	lists := wishlist.NewSteam(wishlist.SteamOptions{HTTPClient: up.Client()})
	defer lists.Close()
	s, db := fixture(t, Options{Lists: lists, GroupURL: up.URL, Token: func(context.Context, string) (string, error) { return "token", nil }})
	bound(t, db, sidA)
	bound(t, db, sidB)
	if _, e := db.Exec(`CREATE TRIGGER reject_first_family BEFORE INSERT ON family_groups WHEN NEW.steamid='76561198000000001' BEGIN SELECT RAISE(ABORT,'reject first account'); END`); e != nil {
		t.Fatal(e)
	}
	v, e := s.Sync(ctx, "")
	if e != nil || v["synced"] != 2 || v["failed"] != 1 {
		t.Fatal(v, e)
	}
	g, e := readGroup(ctx, db, sidB)
	if e != nil || g == nil || !g.FetchOK {
		t.Fatalf("later account did not save %v %v", g, e)
	}
}
