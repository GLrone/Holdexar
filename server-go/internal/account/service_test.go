package account

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/store"
)

func serviceFixture(t *testing.T, handler http.HandlerFunc) (*Service, *sql.DB) {
	t.Helper()
	data := t.TempDir()
	path := filepath.Join(data, "holdexar.db")
	db, e := store.Open(path)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { db.Close() })
	if e = store.InitializeFreshDatabase(context.Background(), db); e != nil {
		t.Fatal(e)
	}
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	client := NewSteamClient(SteamOptions{HTTPClient: srv.Client(), AccountURL: srv.URL + "/account/", ProfileURL: srv.URL + "/miniprofile/%s/json", FinalizeURL: srv.URL + "/finalize"})
	s := NewService(context.Background(), db, store.NewWriteGate(db, path), ServiceOptions{DataDir: data, Steam: client})
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		if e := s.Close(ctx); e != nil {
			t.Error(e)
		}
	})
	return s, db
}
func TestAccountBindingEncryptsAndKeepsPrimaryActiveAndWalletsSeparate(t *testing.T) {
	const a = "76561198000000001"
	const b = "76561198000000002"
	var failing atomic.Bool
	s, db := serviceFixture(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/account/" {
			json.NewEncoder(w).Encode(map[string]any{"persona_name": "fixture", "avatar_url": "", "online": true})
			return
		}
		if failing.Load() {
			w.WriteHeader(429)
			return
		}
		sid := CookieSteamID(r.Header.Get("Cookie"))
		balance := "¥11.00"
		if sid == b {
			balance = "¥22.00"
		}
		fmt.Fprint(w, walletPage(sid, balance, balance, 23))
	})
	ctx := context.Background()
	for _, sid := range []string{a, b} {
		status, e := s.BindCookies(ctx, "Cookie: ignored=drop; steamCountry=US; steamLoginSecure="+sid+"||opaque")
		if e != nil || status["cookie_steam_id"] != sid {
			t.Fatalf("%v %v", status, e)
		}
	}
	status, e := s.Status(ctx)
	if e != nil || status["primary_steam_id"] != a || status["cookie_steam_id"] != b {
		t.Fatalf("%v %v", status, e)
	}
	encoded, _ := json.Marshal(status)
	if strings.Contains(string(encoded), "opaque") || strings.Contains(string(encoded), "steamLoginSecure") {
		t.Fatal("credential in public payload")
	}
	var cipher string
	db.QueryRow(`SELECT cookies FROM steam_accounts WHERE steam_id=?`, a).Scan(&cipher)
	if !strings.HasPrefix(cipher, "enc1:") || strings.Contains(cipher, "opaque") {
		t.Fatal("credential stored in clear")
	}
	if _, e = s.SetActive(ctx, a); e != nil {
		t.Fatal(e)
	}
	status, e = s.Status(ctx)
	wallet := status["wallet"].(map[string]any)
	if e != nil || status["primary_steam_id"] != a || wallet["balance"] != float64(11) || wallet["region_code"] != "" {
		t.Fatalf("%v %v", status, e)
	}
	okAt := wallet["ok_at"]
	failing.Store(true)
	result, e := s.SyncWallet(ctx, a, true)
	if e != nil || result["ok"] != false {
		t.Fatalf("%v %v", result, e)
	}
	status, e = s.Status(ctx)
	wallet = status["wallet"].(map[string]any)
	if e != nil || wallet["balance"] != float64(11) || wallet["check_ok"] != false || wallet["ok_at"] != okAt {
		t.Fatalf("lost good snapshot %v %v", wallet, e)
	}
	var n int
	if e = db.QueryRow(`SELECT count(*) FROM tracked_accounts`).Scan(&n); e != nil || n != 2 {
		t.Fatalf("tracked accounts %d %v", n, e)
	}
}

func TestAccountExpiredWithoutRefreshDoesNotRequestAndFreezes(t *testing.T) {
	var calls atomic.Int32
	s, db := serviceFixture(t, func(w http.ResponseWriter, r *http.Request) { calls.Add(1); w.WriteHeader(401) })
	sid := "76561198000000001"
	status, e := s.BindCookies(context.Background(), "steamLoginSecure="+accessFixture(sid, time.Now().Add(-time.Hour).Unix()))
	if e != nil || status["session_expired"] != true || status["sync_error"] == "" || calls.Load() != 0 {
		t.Fatalf("expired binding calls=%d error=%v", calls.Load(), e)
	}
	var frozen bool
	if e = db.QueryRow(`SELECT wallet_frozen FROM steam_accounts WHERE steam_id=?`, sid).Scan(&frozen); e != nil || !frozen {
		t.Fatalf("%t %v", frozen, e)
	}
}

func TestWalletRotationUsesFreshnessBackoffAndRealScheduler(t *testing.T) {
	var calls atomic.Int32
	var fail atomic.Bool
	s, _ := serviceFixture(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/account/" {
			fmt.Fprint(w, `{"persona_name":"fixture"}`)
			return
		}
		calls.Add(1)
		if fail.Load() {
			w.WriteHeader(429)
			return
		}
		fmt.Fprint(w, walletPage(CookieSteamID(r.Header.Get("Cookie")), "¥11.00", "¥11.00", 23))
	})
	now := time.Now()
	s.opts.Now = func() time.Time { return now }
	s.opts.Sleep = func(context.Context, time.Duration) error { return nil }
	if _, e := s.BindCookies(context.Background(), "steamLoginSecure=76561198000000001||opaque"); e != nil {
		t.Fatal(e)
	}
	now = now.Add(3 * time.Minute)
	result, e := s.Rotate(context.Background())
	if e != nil || calls.Load() != 1 || result["ok_count"] != 1 {
		t.Fatalf("idle cache %v %d %v", result, calls.Load(), e)
	}
	s.MarkSeen()
	clock := sched.NewScheduler()
	if e = s.RegisterSchedules(context.Background(), clock); e != nil {
		t.Fatal(e)
	}
	clock.StartSim(now)
	if e = clock.SetNext("wallet_sync", now.Add(time.Second)); e != nil {
		t.Fatal(e)
	}
	clock.RunSim(now.Add(2 * time.Second))
	if calls.Load() != 2 {
		t.Fatalf("scheduled wallet requests %d", calls.Load())
	}
	fail.Store(true)
	if _, e = s.SyncWallet(context.Background(), "76561198000000001", false); e != nil {
		t.Fatal(e)
	}
	before := calls.Load()
	result, e = s.Rotate(context.Background())
	if e != nil || calls.Load() != before || result["skipped"] != 1 {
		t.Fatalf("backoff %v %v", result, e)
	}
}

func TestWalletSyncKeepsTargetWhenActiveAccountChanges(t *testing.T) {
	const a = "76561198000000001"
	const b = "76561198000000002"
	var block atomic.Bool
	entered, release := make(chan struct{}), make(chan struct{})
	s, _ := serviceFixture(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/account/" {
			w.WriteHeader(404)
			return
		}
		sid := CookieSteamID(r.Header.Get("Cookie"))
		amount := "¥11.00"
		if sid == b {
			amount = "¥22.00"
		}
		if block.Load() && sid == a {
			close(entered)
			select {
			case <-release:
			case <-r.Context().Done():
				return
			}
			amount = "¥33.00"
		}
		fmt.Fprint(w, walletPage(sid, amount, amount, 23))
	})
	ctx := context.Background()
	for _, sid := range []string{a, b} {
		if _, e := s.BindCookies(ctx, "steamLoginSecure="+sid+"||fixture-only"); e != nil {
			t.Fatal(e)
		}
	}
	block.Store(true)
	done := make(chan error, 1)
	go func() { _, e := s.SyncWallet(ctx, a, true); done <- e }()
	select {
	case <-entered:
	case <-time.After(3 * time.Second):
		t.Fatal("wallet fetch did not start")
	}
	if _, e := s.SetActive(ctx, b); e != nil {
		t.Fatal(e)
	}
	close(release)
	if e := <-done; e != nil {
		t.Fatal(e)
	}
	status, e := s.Status(ctx)
	if e != nil || status["cookie_steam_id"] != b || status["wallet"].(map[string]any)["balance"] != float64(22) {
		t.Fatalf("%v %v", status, e)
	}
	accounts, e := s.Accounts(ctx)
	if e != nil || accounts[0]["wallet"].(map[string]any)["balance"] != float64(33) {
		t.Fatalf("target wallet was lost %v", e)
	}
}

func TestRevokedStoreSessionWithoutRefreshRequiresLogin(t *testing.T) {
	s, _ := serviceFixture(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/account/" {
			http.Redirect(w, r, "/login/", 302)
			return
		}
		w.WriteHeader(200)
	})
	status, e := s.BindCookies(context.Background(), "steamLoginSecure=76561198000000001||opaque")
	if e != nil || status["session_expired"] != true || status["sync_error"] != expiredMessage {
		t.Fatalf("%v %v", status, e)
	}
	accounts, e := s.Accounts(context.Background())
	if e != nil || accounts[0]["wallet_frozen"] != true {
		t.Fatalf("%v %v", accounts, e)
	}
}
