package family

import (
	"context"
	"database/sql"
	"encoding/json"
	"path/filepath"
	"testing"

	"holdexar/server-go/internal/store"
)

const sidA = "76561198000000001"
const sidB = "76561198000000002"

func fixture(t *testing.T, opts Options) (*Service, *sql.DB) {
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
	s := New(context.Background(), db, store.NewWriteGate(db, path), opts)
	t.Cleanup(func() {
		if e := s.Close(context.Background()); e != nil {
			t.Error(e)
		}
	})
	return s, db
}
func bound(t *testing.T, db *sql.DB, sid string) {
	t.Helper()
	_, e := db.Exec(`INSERT INTO steam_accounts(steam_id,cookies,persona_name,avatar_url,is_active,is_online,in_game,bound_at,wallet_error,wallet_backoff_level,wallet_fail_streak,wallet_frozen) VALUES(?,'credential-version','bound','',0,0,'','2026-11-01','',0,0,0)`, sid)
	if e != nil {
		t.Fatal(e)
	}
}
func flat(t *testing.T, status map[string]any) []map[string]any {
	t.Helper()
	return status["members"].([]map[string]any)
}

func TestLocalManualMemberSurvivesBindingAndAbsorptionIsAtomic(t *testing.T) {
	s, db := fixture(t, Options{})
	ctx := context.Background()
	if _, e := s.AddMember(ctx, sidB, "Friend", "", ""); e != nil {
		t.Fatal(e)
	}
	state, e := s.Status(ctx)
	if e != nil || state["bound"] != false || len(flat(t, state)) != 1 {
		t.Fatalf("unbound %v %v", state, e)
	}
	bound(t, db, sidA)
	state, e = s.Status(ctx)
	if e != nil || state["bound"] != true || len(flat(t, state)) != 1 {
		t.Fatalf("bound lost local %v %v", state, e)
	}
	if _, e = db.Exec(`CREATE TRIGGER reject_group BEFORE INSERT ON family_groups WHEN NEW.steamid='76561198000000001' BEGIN SELECT RAISE(ABORT,'reject destination'); END`); e != nil {
		t.Fatal(e)
	}
	if _, e = s.AddMember(ctx, sidB, "Friend", "", ""); e == nil {
		t.Fatal("injected failure accepted")
	}
	var raw string
	db.QueryRow(`SELECT members_json FROM family_groups WHERE steamid='local'`).Scan(&raw)
	var members []map[string]any
	json.Unmarshal([]byte(raw), &members)
	if len(members) != 1 {
		t.Fatal("local member lost before destination commit", raw)
	}
	if _, e = db.Exec(`DROP TRIGGER reject_group`); e != nil {
		t.Fatal(e)
	}
	if _, e = s.AddMember(ctx, sidB, "Friend", "", ""); e != nil {
		t.Fatal(e)
	}
	if _, e = s.AddMember(ctx, sidB, "Friend", "", ""); e != nil {
		t.Fatal(e)
	}
	state, e = s.Status(ctx)
	if e != nil || len(flat(t, state)) != 1 {
		t.Fatalf("duplicate absorb %v %v", state, e)
	}
	receipt, e := s.RemoveMember(ctx, sidB, "")
	if e != nil || receipt["removed"] != true {
		t.Fatal(receipt, e)
	}
	state, e = s.Status(ctx)
	if e != nil || len(flat(t, state)) != 0 {
		t.Fatal(state, e)
	}
}
func TestRegionsUseExplicitWalletOrProfileAndKeepMergedManualChanges(t *testing.T) {
	s, db := fixture(t, Options{})
	ctx := context.Background()
	bound(t, db, sidA)
	if _, e := db.Exec(`UPDATE steam_accounts SET wallet_json='{"region_code":"US","currency":"USD"}';INSERT INTO family_groups(steamid,members_json,fetch_ok,member_count,updated_at) VALUES(?,'[{"steamid":"76561198000000001","origin":"steam"},{"steamid":"76561198000000002","origin":"manual","loccountrycode":"UA"},{"steamid":"76561198000000003","origin":"manual"}]',1,1,'2026-11-01')`, sidA); e != nil {
		t.Fatal(e)
	}
	state, e := s.Status(ctx)
	if e != nil {
		t.Fatal(e)
	}
	members := flat(t, state)
	if members[0]["region"] != "us" || members[0]["regionSource"] != "wallet" || members[1]["region"] != "ua" || members[2]["region"] != nil {
		t.Fatal(members)
	}
	if _, e = s.SaveRegions(ctx, map[string]string{sidB: "pk", sidA: "jp"}); e != nil {
		t.Fatal(e)
	}
	if _, e = s.SaveRegions(ctx, map[string]string{sidB: ""}); e != nil {
		t.Fatal(e)
	}
	state, e = s.Status(ctx)
	if e != nil {
		t.Fatal(e)
	}
	members = flat(t, state)
	if members[0]["region"] != "jp" || members[0]["regionSource"] != "manual" || members[1]["region"] != "ua" {
		t.Fatal(members)
	}
	if _, e := db.Exec(`UPDATE steam_accounts SET wallet_json='{"currency":"USD"}'`); e != nil {
		t.Fatal(e)
	}
	if _, e = s.SaveRegions(ctx, map[string]string{sidA: ""}); e != nil {
		t.Fatal(e)
	}
	state, e = s.Status(ctx)
	if e != nil || flat(t, state)[0]["region"] != nil {
		t.Fatalf("guessed wallet region %v %v", state, e)
	}
}
