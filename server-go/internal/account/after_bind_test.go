package account

import (
	"context"
	"fmt"
	"net/http"
	"testing"
)

func TestAfterBindStartsBeforeWalletAndOnlyAfterCommittedIdentity(t *testing.T) {
	const sid = "76561198000000001"
	fired := false
	s, db := serviceFixture(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/account/" {
			if !fired {
				t.Error("after-bind waits for wallet")
			}
			fmt.Fprint(w, walletPage(sid, "¥1.00", "¥1.00", 23))
			return
		}
		fmt.Fprint(w, `{}`)
	})
	s.opts.AfterBind = func(got string) {
		if got != sid {
			t.Errorf("wrong target %q", got)
		}
		var n int
		if e := db.QueryRow(`SELECT COUNT(*) FROM steam_accounts a JOIN tracked_accounts t ON t.steamid=a.steam_id WHERE a.steam_id=?`, got).Scan(&n); e != nil || n != 1 {
			t.Errorf("before commit %d %v", n, e)
		}
		fired = true
	}
	if _, e := s.BindCookies(context.Background(), "steamLoginSecure="+sid+"||fixture"); e != nil || !fired {
		t.Fatalf("hook absent %t %v", fired, e)
	}
	fired = false
	if _, e := s.BindCookies(context.Background(), "invalid"); e == nil || fired {
		t.Fatalf("invalid triggered %t %v", fired, e)
	}
}
