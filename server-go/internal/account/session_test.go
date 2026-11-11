package account

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestRefreshSessionPrefersStoreCookieAndRetainsRefresh(t *testing.T) {
	const sid = "76561198000000001"
	var base string
	seen := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("User-Agent") != "Mozilla/5.0 (Windows NT 10.0; Win64; x64)" || r.Header.Get("Accept-Language") != "zh-CN,zh;q=0.9,en;q=0.8" {
			t.Error("missing browser request headers")
		}
		if r.Method != "POST" || r.ParseMultipartForm(1<<20) != nil {
			t.Error("wrong request")
			w.WriteHeader(400)
			return
		}
		if r.URL.Path == "/finalize" {
			if r.FormValue("nonce") != "refresh-token" || r.FormValue("sessionid") == "" {
				t.Error("missing finalize fields")
			}
			json.NewEncoder(w).Encode(map[string]any{"transfer_info": []any{map[string]any{"url": base + "/store", "params": map[string]any{"nonce": "store"}}, map[string]any{"url": base + "/community", "params": map[string]any{"nonce": "community"}}}})
			return
		}
		if r.FormValue("steamID") != sid {
			t.Error("wrong owner")
		}
		seen++
		http.SetCookie(w, &http.Cookie{Name: "steamLoginSecure", Value: sid + "%7C%7C" + strings.TrimPrefix(r.URL.Path, "/")})
	}))
	defer srv.Close()
	base = srv.URL
	c := NewSteamClient(SteamOptions{HTTPClient: srv.Client(), FinalizeURL: base + "/finalize"})
	raw := "sessionid=old; steamCountry=US; steamLoginSecure=" + sid + "||old; steamRefresh_steam=" + sid + "||refresh-token; steamRememberLogin=true"
	renewed, e := c.RefreshSession(context.Background(), raw)
	if e != nil || CookieSteamID(renewed) != sid || RefreshToken(renewed) != "refresh-token" || seen != 2 {
		t.Fatalf("session refresh failed: %v", e)
	}
	jar := ParseCookies(renewed)
	if jar["steamCountry"] != "US" || jar["steamRememberLogin"] != "true" || jar["sessionid"] == "old" {
		t.Fatal("inherited cookie lost")
	}
	if !strings.HasSuffix(jar["steamLoginSecure"], "store") {
		t.Fatal("store cookie was overwritten")
	}
}

func TestRefreshRejectsUnknownTransferHostAndWrongAccount(t *testing.T) {
	const sid = "76561198000000001"
	mode := "host"
	var base string
	transfers := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/finalize" {
			endpoint := "http://127.0.0.1:1/leak"
			if mode == "owner" {
				endpoint = base + "/transfer"
			}
			fmt.Fprintf(w, `{"transfer_info":[{"url":%q,"params":{"auth":"fixture-only"}}]}`, endpoint)
			return
		}
		transfers++
		http.SetCookie(w, &http.Cookie{Name: "steamLoginSecure", Value: "76561198000000002||wrong"})
	}))
	defer srv.Close()
	base = srv.URL
	c := NewSteamClient(SteamOptions{HTTPClient: srv.Client(), FinalizeURL: base + "/finalize", Sleep: func(context.Context, time.Duration) error { return nil }})
	for _, value := range []string{"host", "owner"} {
		mode = value
		if _, e := c.RefreshSession(context.Background(), "steamLoginSecure="+sid+"||old; steamRefresh_steam="+sid+"||refresh"); e == nil {
			t.Fatal("unsafe renewal accepted")
		}
	}
	if transfers != 1 {
		t.Fatal("unexpected transfer calls", transfers)
	}
}
