package account

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestAccountHTTPBindsAndShowsRealWalletWithoutCredentials(t *testing.T) {
	s, _ := serviceFixture(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/account/" {
			w.WriteHeader(404)
			return
		}
		w.Write([]byte(walletPage(CookieSteamID(r.Header.Get("Cookie")), "¥42.50", "¥42.50", 23)))
	})
	mux := http.NewServeMux()
	s.Register(mux)
	request := func(method, path, body string) (int, map[string]any) {
		w := httptest.NewRecorder()
		mux.ServeHTTP(w, httptest.NewRequest(method, path, strings.NewReader(body)))
		var result map[string]any
		if e := json.Unmarshal(w.Body.Bytes(), &result); e != nil {
			t.Fatal(w.Body.String())
		}
		return w.Code, result
	}
	code, status := request("GET", "/api/v1/account", "")
	if code != 200 || status["has_cookie"] != false {
		t.Fatalf("%d %v", code, status)
	}
	code, status = request("PUT", "/api/v1/account/cookies", `{"cookies":"steamLoginSecure=76561198000000001||opaque"}`)
	if code != 200 || status["wallet"].(map[string]any)["balance"] != 42.5 || strings.Contains(mustJSON(status), "opaque") {
		t.Fatalf("%d %v", code, status)
	}
	code, _ = request("PUT", "/api/v1/account/active", `{"steam_id":"missing"}`)
	if code != 404 {
		t.Fatal(code)
	}
	code, _ = request("PUT", "/api/v1/account/cookies", `{"cookies":null}`)
	if code != 422 {
		t.Fatal(code)
	}
	code, status = request("POST", "/api/v1/account/sync", "")
	if code != 200 || status["sync_error"] != "" {
		t.Fatalf("%d %v", code, status)
	}
}
func mustJSON(value any) string { raw, _ := json.Marshal(value); return string(raw) }
