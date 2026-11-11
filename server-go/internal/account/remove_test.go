package account

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"holdexar/server-go/internal/monitoring"
)

func removalFixture(t *testing.T) *Service {
	t.Helper()
	s, db := serviceFixture(t, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(429) })
	for _, sid := range []string{"76561198000000001", "76561198000000002"} {
		if _, e := s.BindCookies(context.Background(), "steamLoginSecure="+sid+"||fixture"); e != nil {
			t.Fatal(e)
		}
	}
	_, e := db.Exec(`INSERT INTO bill_imports(id,steam_id,nickname,avatar_base64,source_file,warnings_json) VALUES
 (1,'76561198000000001','A','','','[]'),(2,'76561198000000002','B','','','[]'),(3,NULL,'Legacy','','','[]');
 INSERT INTO bill_game_txs(id,import_id,date,tx_type,items_json,currency,amount,sign,fx_note,payment,discount_pct,wallet_balance,base_price,is_gift,is_refund,orig_is_gift,raw_idx)
 SELECT id,id,'2026-11-01','purchase','[]','CNY',10,-1,'','','','','',0,0,0,0 FROM bill_imports;
 INSERT INTO bill_topup_txs(id,import_id,date,"desc",currency,amount,sign,fx_note,payment,tx_type,is_refund)
 SELECT id,id,'2026-11-01','wallet','CNY',10,1,'','','topup',0 FROM bill_imports;
 INSERT INTO bill_cdk_games(id,import_id,name,date,acq,acq_label) SELECT id,id,'fixture','2026-11-01','key','key' FROM bill_imports;
 INSERT INTO family_groups(steamid,family_groupid,fetch_ok,member_count,updated_at) VALUES
 ('76561198000000001','shared',1,2,'2026-11-01'),('76561198000000002','shared',1,2,'2026-11-01');
 INSERT INTO family_library_snapshots(appid,family_groupid,time_acquired,presence,excluded,updated_at) VALUES
 (620,'shared',1,1,0,'2026-11-01'),(570,'orphan',1,1,0,'2026-11-01');
 INSERT INTO tracked_accounts(steamid,kinds_json) VALUES('manual-only','{"wishlist":true}');
 INSERT INTO wishlist_items(steamid,appid,active,owned,manual,wishlisted,manual_pool,excluded,board_pool) VALUES
 ('76561198000000001',10,1,0,0,1,0,0,0),('76561198000000001',20,1,1,0,0,0,0,0),
 ('76561198000000001',30,1,0,0,1,0,0,0),('76561198000000002',20,1,0,0,1,0,0,0),
 ('manual-only',40,1,1,0,0,0,0,0);
 INSERT INTO app_settings(key,value_json) VALUES('account.steam_cookies','"legacy-credential"'),('account.steam_id','"manual-identity"'),('account.steam_api_key','"fixture-key"');`)
	if e != nil {
		t.Fatal(e)
	}
	ctx := context.Background()
	_, e = s.opts.Pool.Monitor.Apply(ctx, "game", []int64{10, 20, 30, 40, 50}, monitoring.Change{AddOnly: true, Sources: map[int64][]string{
		10: {"family_wishlist"}, 20: {"owned"}, 30: {"family_wishlist", "favorite", "board"}, 40: {"owned"}, 50: {"owned"},
	}})
	if e != nil {
		t.Fatal(e)
	}
	return s
}

func TestRemoveAccountPurgesOnlyItsDataAndReconcilesSharedSources(t *testing.T) {
	s := removalFixture(t)
	ctx := context.Background()
	receipt, e := s.RemoveAccount(ctx, "76561198000000001")
	if e != nil || receipt["billsRemoved"] != 1 || receipt["familyGroupsRemoved"] != 1 || receipt["familySnapshotsRemoved"] != 1 || receipt["trackingRemoved"] != 3 || receipt["poolReleased"] != 2 {
		t.Fatalf("receipt=%v error=%v", receipt, e)
	}
	status, e := s.Status(ctx)
	if e != nil || status["primary_steam_id"] != "76561198000000002" || status["cookie_steam_id"] != "76561198000000002" {
		t.Fatalf("status=%v error=%v", status, e)
	}
	for _, table := range []string{"bill_imports", "bill_game_txs", "bill_topup_txs", "bill_cdk_games"} {
		var count int
		if e := s.db.QueryRow("SELECT count(*) FROM " + table + " WHERE id IN(2,3)").Scan(&count); e != nil || count != 2 {
			t.Fatalf("other and unowned %s count=%d error=%v", table, count, e)
		}
	}
	var snaps int
	if e := s.db.QueryRow(`SELECT count(*) FROM family_library_snapshots WHERE family_groupid='shared'`).Scan(&snaps); e != nil || snaps != 1 {
		t.Fatalf("shared family snapshot lost: %d %v", snaps, e)
	}
	for id, want := range map[int64]string{10: "released", 20: "active", 30: "active", 40: "active", 50: "released"} {
		snapshot, e := s.opts.Pool.Monitor.Snapshot(ctx, "game", id)
		if e != nil || snapshot["state"] != want {
			t.Fatalf("id=%d snapshot=%v error=%v", id, snapshot, e)
		}
		if id == 20 && strings.Join(snapshot["sources"].([]string), ",") != "family_wishlist" {
			t.Fatalf("shared account flags not reconciled: %v", snapshot)
		}
		if id == 30 && strings.Join(snapshot["sources"].([]string), ",") != "board,favorite" {
			t.Fatalf("explicit sources changed: %v", snapshot)
		}
	}
	if _, e = s.RemoveAccount(ctx, "76561198000000001"); !errors.Is(e, ErrAccountNotFound) {
		t.Fatalf("missing account error=%v", e)
	}
}

func TestClearCookiesPreservesManualIdentityTrackingAndLegacyBills(t *testing.T) {
	s := removalFixture(t)
	ctx := context.Background()
	if e := s.ClearCookies(ctx); e != nil {
		t.Fatal(e)
	}
	status, e := s.Status(ctx)
	if e != nil || status["has_cookie"] != false || len(status["accounts"].([]map[string]any)) != 0 || status["bound_steam_id"] != "manual-identity" {
		t.Fatalf("%v %v", status, e)
	}
	for _, query := range []string{
		`SELECT count(*) FROM tracked_accounts WHERE steamid='manual-only'`,
		`SELECT count(*) FROM wishlist_items WHERE steamid='manual-only'`,
		`SELECT count(*) FROM bill_imports WHERE id=3 AND steam_id IS NULL`,
		`SELECT count(*) FROM bill_game_txs WHERE import_id=3`,
		`SELECT count(*) FROM app_settings WHERE key='account.steam_cookies' AND value_json='""'`,
		`SELECT count(*) FROM app_settings WHERE key='account.steam_api_key' AND value_json='"fixture-key"'`,
	} {
		var count int
		if e = s.db.QueryRow(query).Scan(&count); e != nil || count != 1 {
			t.Fatalf("preservation count=%d error=%v query=%s", count, e, query)
		}
	}
	var groups, snaps int
	s.db.QueryRow(`SELECT count(*) FROM family_groups`).Scan(&groups)
	s.db.QueryRow(`SELECT count(*) FROM family_library_snapshots`).Scan(&snaps)
	if groups != 0 || snaps != 0 {
		t.Fatalf("remaining groups=%d snapshots=%d", groups, snaps)
	}
	if e = s.ClearCookies(ctx); e != nil {
		t.Fatal("empty unbind must remain idempotent", e)
	}
}

func TestRemoveAccountRollsBackOnTrackingWriteFailure(t *testing.T) {
	s := removalFixture(t)
	if _, e := s.db.Exec(`CREATE TRIGGER reject_tracking_delete BEFORE DELETE ON wishlist_items BEGIN SELECT RAISE(ABORT,'controlled failure'); END`); e != nil {
		t.Fatal(e)
	}
	if _, e := s.RemoveAccount(context.Background(), "76561198000000001"); e == nil {
		t.Fatal("delete failure swallowed")
	}
	for _, query := range []string{
		`SELECT count(*) FROM steam_accounts WHERE steam_id='76561198000000001'`,
		`SELECT count(*) FROM bill_imports WHERE id=1`,
		`SELECT count(*) FROM family_groups WHERE steamid='76561198000000001'`,
		`SELECT count(*) FROM tracked_accounts WHERE steamid='76561198000000001'`,
	} {
		var count int
		if e := s.db.QueryRow(query).Scan(&count); e != nil || count != 1 {
			t.Fatalf("partial deletion count=%d error=%v", count, e)
		}
	}
}

func TestAccountHTTPDeletesOneAndAllWithOriginalStatusContract(t *testing.T) {
	s := removalFixture(t)
	mux := http.NewServeMux()
	s.Register(mux)
	for _, test := range []struct {
		path   string
		code   int
		active string
	}{{"/api/v1/account/cookies/76561198000000002", 200, "76561198000000001"}, {"/api/v1/account/cookies/76561198000000002", 404, ""}, {"/api/v1/account/cookies", 200, ""}} {
		w := httptest.NewRecorder()
		mux.ServeHTTP(w, httptest.NewRequest("DELETE", test.path, nil))
		if w.Code != test.code || test.code == 200 && !strings.Contains(w.Body.String(), `"has_cookie"`) {
			t.Fatalf("%s status=%d body=%s", test.path, w.Code, w.Body.String())
		}
		if test.code == 200 {
			var status map[string]any
			if e := json.Unmarshal(w.Body.Bytes(), &status); e != nil || status["cookie_steam_id"] != test.active || status["has_cookie"] != (test.active != "") {
				t.Fatalf("active fallback=%v error=%v", status, e)
			}
		}
	}
}

func TestClearCookiesWhileWalletInFlightCannotRestoreDeletedAccount(t *testing.T) {
	entered, release := make(chan struct{}), make(chan struct{})
	var block atomic.Bool
	s, db := serviceFixture(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/account/" {
			w.WriteHeader(404)
			return
		}
		if block.Load() {
			close(entered)
			<-release
		}
		fmt.Fprint(w, walletPage(CookieSteamID(r.Header.Get("Cookie")), "¥11.00", "¥11.00", 23))
	})
	t.Cleanup(func() { close(release) })
	ctx := context.Background()
	if _, e := s.BindCookies(ctx, "steamLoginSecure=76561198000000001||fixture"); e != nil {
		t.Fatal(e)
	}
	block.Store(true)
	done := make(chan error, 1)
	go func() { _, e := s.SyncWallet(ctx, "76561198000000001", true); done <- e }()
	select {
	case <-entered:
	case <-time.After(3 * time.Second):
		t.Fatal("wallet request never started")
	}
	if e := s.ClearCookies(ctx); e != nil {
		t.Fatal(e)
	}
	release <- struct{}{}
	if e := <-done; e == nil {
		t.Fatal("late wallet result was accepted")
	}
	for _, table := range []string{"steam_accounts", "tracked_accounts", "wishlist_items"} {
		var count int
		if e := db.QueryRow("SELECT count(*) FROM " + table).Scan(&count); e != nil || count != 0 {
			t.Fatalf("late %s count=%d error=%v", table, count, e)
		}
	}
	result, e := s.Rotate(ctx)
	if e != nil || result["status"] != "no_cookie" {
		t.Fatalf("removed account was scheduled: %v %v", result, e)
	}
}
