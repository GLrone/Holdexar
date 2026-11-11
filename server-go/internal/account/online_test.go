package account

import (
	"context"
	"database/sql"
	"fmt"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
	"holdexar/server-go/internal/wishlist"
)

func TestOfficialOnlineRotationTargetsOnlyStaleAccounts(t *testing.T) {
	const a, b, c = "76561198000000001", "76561198000000002", "76561198000000003"
	const fixtureKey = "synthetic-online-fixture"
	var requests, mode atomic.Int32
	s, db := serviceFixture(t, func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/account/":
			sid := CookieSteamID(r.Header.Get("Cookie"))
			fmt.Fprint(w, walletPage(sid, "¥10.00", "¥10.00", 23))
		case "/official":
			requests.Add(1)
			if r.Header.Get("Cookie") != "" || r.URL.Query().Get("key") != fixtureKey || r.URL.Query().Get("steamids") != b+","+c {
				t.Errorf("online request used the wrong accounts or credentials")
			}
			switch mode.Load() {
			case 1:
				http.Error(w, "controlled failure", 403)
			case 2:
				fmt.Fprint(w, `{"response":{"players":[]}}`)
			default:
				fmt.Fprintf(w, `{"response":{"players":[{"steamid":%q,"personastate":"3","gameextrainfo":"Official game"},{"steamid":%q,"personastate":0}]}}`, b, a)
			}
		default:
			fmt.Fprint(w, `{"persona_name":"Fallback","online":true,"in_game":{"name":"Public game"}}`)
		}
	})
	ctx := context.Background()
	now := time.Date(2026, 11, 7, 6, 0, 0, 0, time.UTC)
	s.opts.Now = func() time.Time { return now }
	url := strings.TrimSuffix(s.opts.Steam.opts.AccountURL, "/account/") + "/official"
	api := wishlist.NewSteam(wishlist.SteamOptions{HTTPClient: s.opts.Steam.opts.HTTPClient})
	s.opts.Summaries = func(ctx context.Context, ids []string, key string) (map[string]wishlist.PlayerSummary, error) {
		return api.PlayerSummaries(ctx, url, ids, key)
	}
	for _, sid := range []string{a, b, c} {
		if _, err := s.BindCookies(ctx, "steamLoginSecure="+sid+"||fixture"); err != nil {
			t.Fatal(err)
		}
	}
	if err := s.gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		_, err := conn.ExecContext(ctx, `UPDATE steam_accounts SET in_game='Fresh untouched',online_checked_at=? WHERE steam_id=?`, accountStamp(s.now()), a)
		return err
	}); err != nil {
		t.Fatal(err)
	}
	s.MarkSeen()
	if _, err := s.Rotate(ctx); err != nil || requests.Load() != 0 {
		t.Fatalf("made official calls without a configured key: %d %v", requests.Load(), err)
	}
	s.opts.APIKey = func(context.Context) (string, error) { return fixtureKey, nil }
	if _, err := s.Rotate(ctx); err != nil || requests.Load() != 1 {
		t.Fatalf("wanted one official batch: %d %v", requests.Load(), err)
	}
	check := func(sid, game string, online bool) {
		t.Helper()
		var got string
		var active bool
		if err := db.QueryRow(`SELECT is_online,in_game FROM steam_accounts WHERE steam_id=?`, sid).Scan(&active, &got); err != nil || got != game || active != online {
			t.Fatalf("%s state %t %q err=%v", sid, active, got, err)
		}
	}
	check(a, "Fresh untouched", true)
	check(b, "Official game", true)
	check(c, "", false)
	if _, err := s.Rotate(ctx); err != nil || requests.Load() != 1 {
		t.Fatalf("queried already fresh states: %d %v", requests.Load(), err)
	}
	rows, err := s.rows(ctx)
	if err != nil || len(rows) != 3 {
		t.Fatal(rows, err)
	}
	for _, stage := range []int32{1, 2} {
		mode.Store(stage)
		_ = s.refreshOnline(ctx, rows[1:])
		check(b, "Official game", true)
		check(c, "", false)
	}
}
