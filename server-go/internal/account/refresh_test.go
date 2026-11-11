package account

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
)

func TestAccountRefreshQueuesSelectedOwnerAfterWallet(t *testing.T) {
	const a, b = "76561198000000001", "76561198000000002"
	var phase atomic.Int32
	s, db := serviceFixture(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/account/" {
			fmt.Fprint(w, `{"persona_name":"Fixture","avatar_url":"https://avatars.steamstatic.com/a_medium.jpg"}`)
			return
		}
		sid := CookieSteamID(r.Header.Get("Cookie"))
		balance := "¥11.00"
		if sid == b {
			balance = "¥22.00"
		}
		if phase.Load() != 0 {
			if sid != b {
				t.Error("refresh borrowed another account's wallet credential")
			}
			if phase.Load() == 2 {
				http.Error(w, "controlled wallet failure", 403)
				return
			}
			balance = "¥37.25"
		}
		fmt.Fprint(w, walletPage(sid, balance, balance, 23))
	})
	ctx := context.Background()
	for _, sid := range []string{b, a} {
		if _, err := s.BindCookies(ctx, "steamLoginSecure="+sid+"||fixture"); err != nil {
			t.Fatal(err)
		}
	}
	var queued []string
	s.opts.AfterBind = func(sid string) {
		var amount float64
		if err := db.QueryRow(`SELECT json_extract(wallet_json,'$.balance') FROM steam_accounts WHERE steam_id=?`, sid).Scan(&amount); err != nil || amount != 37.25 {
			t.Errorf("jobs queued before the selected wallet was written: %v %v", amount, err)
		}
		queued = append(queued, sid)
	}
	mux := http.NewServeMux()
	s.Register(mux)
	request := func(sid string, status int) map[string]any {
		t.Helper()
		w := httptest.NewRecorder()
		mux.ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/api/v1/account/steam_id/"+sid+"/refresh", nil))
		var value map[string]any
		if w.Code != status || json.Unmarshal(w.Body.Bytes(), &value) != nil {
			t.Fatalf("%s response: %d %s", sid, w.Code, w.Body.String())
		}
		return value
	}
	request("invalid", 400)
	request("76561198000000003", 404)
	if len(queued) != 0 {
		t.Fatal("invalid refresh started jobs")
	}
	phase.Store(1)
	value := request(b, 200)
	wallet := value["wallet"].(map[string]any)
	if value["ok"] != true || value["steamId"] != b || wallet["wallet"].(map[string]any)["balance"] != 37.25 || !reflect.DeepEqual(value["queued"], []any{"wishlist", "family", "bills"}) || wallet["cached"] != nil {
		t.Fatalf("unexpected acceptance: %v", value)
	}
	phase.Store(2)
	value = request(b, 200)
	if value["ok"] != false || !strings.Contains(textField(value["wallet"].(map[string]any)["error"]), "403") || !reflect.DeepEqual(queued, []string{b, b}) {
		t.Fatalf("failed wallet lost the queued work: %v %v", value, queued)
	}
	status, err := s.Status(ctx)
	if err != nil || status["cookie_steam_id"] != a || status["wallet"].(map[string]any)["balance"] != 11.0 {
		t.Fatalf("the active account was changed: %v %v", status, err)
	}
}
