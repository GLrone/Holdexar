package wishlist

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/cookiejar"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

func TestSteamListClientSeparatesTargetTokensAndFallsBackWithoutLeakingSecrets(t *testing.T) {
	var jwtRejected atomic.Bool
	var ownedRequests atomic.Int32
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		if r.Header.Get("Cookie") != "" {
			t.Error("API query acquired a shared cookie jar")
		}
		switch r.URL.Path {
		case "/wish":
			fmt.Fprint(w, `{"response":{"items":[{"appid":620,"added_at":1700000000},{"appid":620},{"appid":0}]}}`)
		case "/owned":
			ownedRequests.Add(1)
			if q.Get("steamid") != "76561198000000002" || q.Get("include_appinfo") != "1" || q.Get("include_played_free_games") != "1" {
				t.Error("owned request target or flags changed")
			}
			if q.Get("access_token") != "" {
				if q.Get("access_token") != "token-for-76561198000000002" || q.Get("key") != "" {
					t.Error("credential owner mixed")
				}
				if jwtRejected.Load() {
					w.WriteHeader(401)
					return
				}
			} else if q.Get("key") != "fixture-web-key" {
				t.Error("key fallback missing")
			}
			fmt.Fprint(w, `{"response":{"games":[{"appid":620,"name":"Portal 2"}]}}`)
		case "/resolve":
			if q.Get("key") != "fixture-web-key" || q.Get("vanityurl") != "fixture" {
				t.Error("vanity resolution fields")
			}
			fmt.Fprint(w, `{"response":{"success":1,"steamid":"76561198000000002"}}`)
		}
	}))
	defer provider.Close()
	httpClient := provider.Client()
	httpClient.Jar, _ = cookiejar.New(nil)
	request, _ := http.NewRequest("GET", provider.URL, nil)
	httpClient.Jar.SetCookies(request.URL, []*http.Cookie{{Name: "steamLoginSecure", Value: "wrong-shared-cookie"}})
	client := NewSteam(SteamOptions{HTTPClient: httpClient, WishlistURL: provider.URL + "/wish", OwnedURL: provider.URL + "/owned", ResolveURL: provider.URL + "/resolve", Token: func(_ context.Context, sid string) (string, error) { return "token-for-" + sid, nil }, APIKey: func(context.Context) (string, error) { return "fixture-web-key", nil }})
	t.Cleanup(client.Close)
	ctx := context.Background()
	wish, e := client.Wishlist(ctx, "76561198000000002")
	if e != nil || len(wish) != 1 || wish[0].AppID != 620 || wish[0].AddedAt != 1700000000 {
		t.Fatalf("wishlist=%v error=%v", wish, e)
	}
	owned, source, e := client.Owned(ctx, "76561198000000002")
	if e != nil || len(owned) != 1 || source != "jwt" {
		t.Fatalf("owned=%v source=%s error=%v", owned, source, e)
	}
	jwtRejected.Store(true)
	owned, source, e = client.Owned(ctx, "76561198000000002")
	if e != nil || len(owned) != 1 || source != "key" || ownedRequests.Load() != 3 {
		t.Fatalf("fallback=%v source=%s error=%v calls=%d", owned, source, e, ownedRequests.Load())
	}
	for _, raw := range []string{"76561198000000002", "39734274", "https://steamcommunity.com/profiles/76561198000000002/", "https://steamcommunity.com/id/fixture/", "fixture"} {
		id, e := client.Resolve(ctx, raw)
		if e != nil || id != "76561198000000002" {
			t.Fatalf("resolve %s=%s error=%v", raw, id, e)
		}
	}
	if _, e = client.Resolve(ctx, "https://evil.example/steamcommunity.com/profiles/76561198000000002"); e == nil {
		t.Fatal("untrusted profile URL accepted")
	}
}

func TestOwnedFailureIsNotAnEmptyLibraryAndErrorsContainNoCredentials(t *testing.T) {
	var status atomic.Int32
	var malformed atomic.Bool
	status.Store(403)
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if status.Load() != 200 {
			w.WriteHeader(int(status.Load()))
			return
		}
		if malformed.Load() {
			fmt.Fprint(w, `{"response":{"games":[]}}{}`)
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"response": map[string]any{"games": []any{}}})
	}))
	defer provider.Close()
	client := NewSteam(SteamOptions{HTTPClient: provider.Client(), OwnedURL: provider.URL, Token: func(context.Context, string) (string, error) { return "private-fixture-token", nil }})
	t.Cleanup(client.Close)
	if _, _, e := client.Owned(context.Background(), "76561198000000001"); e == nil || strings.Contains(e.Error(), "private-fixture-token") || strings.Contains(e.Error(), provider.URL) {
		t.Fatalf("owned denial or secret handling error=%v", e)
	}
	status.Store(200)
	if games, source, e := client.Owned(context.Background(), "76561198000000001"); e != nil || len(games) != 0 || source != "jwt" {
		t.Fatalf("legitimate empty library=%v source=%s error=%v", games, source, e)
	}
	malformed.Store(true)
	if _, _, e := client.Owned(context.Background(), "76561198000000001"); e == nil {
		t.Fatal("malformed response was treated as an empty library")
	}
}
