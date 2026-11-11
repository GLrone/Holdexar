package achievements

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

const steamTestOwner = "76561198171842699"

type steamTestTransport func(*http.Request) (*http.Response, error)

func (f steamTestTransport) RoundTrip(req *http.Request) (*http.Response, error) { return f(req) }

func steamTestClient(t *testing.T, handler http.HandlerFunc) *Steam {
	t.Helper()
	server := httptest.NewTLSServer(handler)
	t.Cleanup(server.Close)
	base, err := url.Parse(server.URL)
	if err != nil {
		t.Fatal(err)
	}
	client := server.Client()
	transport := client.Transport
	client.Transport = steamTestTransport(func(req *http.Request) (*http.Response, error) {
		copy := req.Clone(req.Context())
		copy.URL.Host = base.Host
		return transport.RoundTrip(copy)
	})
	return NewSteam(SteamOptions{HTTPClient: client})
}

func TestSteamOwnedAndProgressUseSelectedIdentityAndBoundedForm(t *testing.T) {
	var batches []int
	steam := steamTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/IPlayerService/GetOwnedGames/v1/":
			q := r.URL.Query()
			if q.Get("steamid") != steamTestOwner || q.Get("include_appinfo") != "1" || q.Get("include_played_free_games") != "1" {
				t.Errorf("wrong owned query for target or F2P")
			}
			if q.Get("key") == "test-key" {
				w.WriteHeader(403)
				return
			}
			if q.Get("access_token") != "test-token" {
				t.Errorf("owned did not fall back to selected token")
			}
			fmt.Fprint(w, `{"response":{"game_count":1,"games":[{"appid":620,"name":"Portal 2","playtime_forever":85,"rtime_last_played":123}]}}`)
		case "/IPlayerService/GetAchievementsProgress/v1/":
			if r.Method != "POST" || r.URL.RawQuery != "" || r.ParseForm() != nil || r.PostForm.Get("steamid") != steamTestOwner {
				t.Error("progress parameters were not sent as a POST form")
			}
			if r.PostForm.Get("key") == "test-key" {
				w.WriteHeader(401)
				return
			}
			if r.PostForm.Get("access_token") != "test-token" || r.PostForm.Get("key") != "" {
				t.Error("progress credentials were not selected independently")
			}
			var ids []string
			for i := range len(r.PostForm) - 2 {
				ids = append(ids, r.PostForm.Get(fmt.Sprintf("appids[%d]", i)))
			}
			batches = append(batches, len(ids))
			rows := make([]map[string]any, 0, len(ids))
			for _, id := range ids {
				rows = append(rows, map[string]any{"appid": id, "total": 5, "unlocked": 2})
			}
			rows = append(rows, map[string]any{"appid": 3000, "total": 8, "unlocked": 4})
			_ = json.NewEncoder(w).Encode(map[string]any{"response": map[string]any{"achievement_progress": rows}})
		default:
			t.Errorf("unexpected endpoint %s", r.URL.Path)
			w.WriteHeader(404)
		}
	})
	ctx := context.Background()
	creds := Credentials{Key: "test-key", Token: "test-token"}
	owned, err := steam.Owned(ctx, steamTestOwner, creds)
	if err != nil || len(owned) != 1 || owned[0] != (OwnedGame{AppID: 620, Name: "Portal 2", Playtime: 85, LastPlayed: 123}) {
		t.Fatalf("owned = %#v, %v", owned, err)
	}
	ids := make([]int64, 101)
	for i := range ids {
		ids[i] = int64(i + 1)
	}
	progress, err := steam.Progress(ctx, steamTestOwner, creds, ids)
	if err != nil || fmt.Sprint(batches) != "[100 1]" || len(progress) != 101 || progress[101] != (Progress{Total: 5, Unlocked: 2}) {
		t.Fatalf("progress batch sizes %v, rows %d, last %#v, err %v", batches, len(progress), progress[101], err)
	}
}

func TestSteamAPIDefinitionsAndStatesUseStableImageKeys(t *testing.T) {
	steam := steamTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("key") != "test-key" {
			t.Error("missing API key")
		}
		switch r.URL.Path {
		case "/IPlayerService/GetGameAchievements/v1/":
			fmt.Fprint(w, `{"response":{"achievements":[{"internal_name":"LATER","localized_name":"Later","icon":"SHARED.jpg","player_percent_unlocked":"10.2"},{"internal_name":"FIRST","localized_name":"First","icon":"SHARED.jpg","icon_gray":"SHARED_BW.jpg","player_percent_unlocked":91.4}]}}`)
		case "/ISteamUserStats/GetPlayerAchievements/v1/":
			fmt.Fprint(w, `{"playerstats":{"steamID":"`+steamTestOwner+`","success":true,"achievements":[{"apiname":"LATER","achieved":1,"unlocktime":123},{"apiname":"FIRST","achieved":0,"unlocktime":0},{"apiname":"UNKNOWN","achieved":1}]}}`)
		default:
			t.Errorf("unexpected endpoint %s", r.URL.Path)
			w.WriteHeader(404)
		}
	})
	ctx := context.Background()
	creds := Credentials{Key: "test-key", Token: "test-token"}
	defs, err := steam.Definitions(ctx, 620, creds)
	if err != nil || len(defs) != 2 || defs[0].ImageName != "SHARED" || defs[0].APIName != "FIRST" || defs[1].ImageName != "SHARED#1" || defs[1].Percent == nil || *defs[1].Percent != 10.2 || !strings.HasSuffix(defs[0].GrayIconURL, "/620/SHARED.jpg") {
		t.Fatalf("definitions = %#v, %v", defs, err)
	}
	raw, err := steam.PlayerStates(ctx, steamTestOwner, 620, creds)
	if err != nil || len(raw) != 3 || raw["LATER"].UnlockTime != 123 {
		t.Fatalf("raw states = %#v, %v", raw, err)
	}
	states, err := steam.States(ctx, steamTestOwner, 620, creds, defs)
	if err != nil || len(states) != 2 || states["SHARED"].Achieved || states["SHARED#1"] != (State{Achieved: true, UnlockTime: 123}) {
		t.Fatalf("matched states = %#v, %v", states, err)
	}
}

func TestSteamCurrentCommunityPayloadDistinguishesPrivateFromLocked(t *testing.T) {
	steam := steamTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("l") != "schinese" || r.URL.Query().Get("key") != "" || r.URL.Query().Get("access_token") != "" || r.Header.Get("Cookie") != "" {
			t.Error("unexpected locale or credentials on public page")
		}
		if strings.HasPrefix(r.URL.Path, "/stats/") {
			http.Redirect(w, r, "/achievements/620", http.StatusFound)
			return
		}
		schema := `{"queryKey":["achievements",620,"schema_v2"],"state":{"status":"success","data":{"appid":620,"groups":[{"ispublic":true,"achievements":[{"internal_key":1,"api_name":"PLAY","name":"游玩 & 解锁","description":"开始","icon_achieved":"A.jpg","icon_unachieved":"A_BW.jpg"},{"internal_key":2,"api_name":"WIN","name":"获胜","icon_achieved":"B.jpg","icon_unachieved":"B_BW.jpg"}]}]}}}`
		percent := `{"queryKey":["achievements",620,"globalpercentages"],"state":{"status":"success","data":{"percentages":{"1":91.800003,"2":23.4}}}}`
		queries := schema + "," + percent
		if strings.Contains(r.URL.Path, steamTestOwner) {
			queries += `,{"queryKey":["achievements",620,"user_achievements","` + steamTestOwner + `"],"state":{"status":"success","data":{"appid":620,"steamid":"` + steamTestOwner + `","achievements":{"1":{"internal_key":1,"unlocked":true,"unlock_time":123}}}}}`
		}
		data, _ := json.Marshal(map[string]any{"renderContext": map[string]string{"queryData": `{"queries":[` + queries + `]}`}})
		fmt.Fprintf(w, `<html><script id="valve-ssr-data" type="application/json">%s</script></html>`, data)
	})
	ctx := context.Background()
	defs, err := steam.Definitions(ctx, 620, Credentials{})
	if err != nil || len(defs) != 2 || defs[0].APIName != "PLAY" || defs[0].Name != "游玩 & 解锁" || defs[0].Percent == nil || *defs[0].Percent != 91.8 {
		t.Fatalf("public definitions = %#v, %v", defs, err)
	}
	states, err := steam.States(ctx, steamTestOwner, 620, Credentials{}, defs)
	if err != nil || len(states) != 2 || !states["A"].Achieved || states["A"].UnlockTime != 123 || states["B"].Achieved {
		t.Fatalf("public player states = %#v, %v", states, err)
	}
	states, err = steam.States(ctx, "76561197960265728", 620, Credentials{}, defs)
	if err == nil || len(states) != 0 {
		t.Fatalf("missing private user data became locked states = %#v, %v", states, err)
	}
}

func TestSteamLegacyCommunityMatchesDuplicateIconsAndNames(t *testing.T) {
	steam := steamTestClient(t, func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, "/stats/") {
			fmt.Fprint(w, `<div class="achieveRow "><img src="https://shared.fastly.steamstatic.com/community_assets/images/apps/620/SHARED.jpg"><h3>One</h3><h5>Desc &amp; more</h5><div class="achievePercent">90%</div></div><div class="achieveRow"><img src="https://shared.fastly.steamstatic.com/community_assets/images/apps/620/SHARED.jpg"><h3>Two</h3></div><div class="achieveRow"><img src="https://shared.fastly.steamstatic.com/community_assets/images/apps/620/OTHER.jpg"><h3>Three</h3></div>`)
			return
		}
		fmt.Fprint(w, `<div class="achieveRow"><img src="https://shared.fastly.steamstatic.com/community_assets/images/apps/620/SHARED_BW.jpg"><h3>One</h3></div><div class="achieveRow"><img src="https://shared.fastly.steamstatic.com/community_assets/images/apps/620/SHARED.jpg"><h3>Two</h3><div class="achieveUnlockTime">2024 年 11 月 2 日 下午 3:56 解锁</div></div><div class="achieveRow"><img src="https://shared.fastly.steamstatic.com/community_assets/images/apps/620/DIFFERENT.jpg"><h3>Three</h3><div class="achieveUnlockTime">Unlocked Feb 11, 2016 @ 1:11am</div></div>`)
	})
	ctx := context.Background()
	defs, err := steam.Definitions(ctx, 620, Credentials{})
	if err != nil || len(defs) != 3 || defs[0].Description != "Desc & more" || defs[1].ImageName != "SHARED#1" {
		t.Fatalf("legacy definitions = %#v, %v", defs, err)
	}
	states, err := steam.States(ctx, steamTestOwner, 620, Credentials{}, defs)
	if err != nil || len(states) != 3 || states["SHARED"].Achieved || !strings.HasSuffix(states["SHARED"].GrayIconURL, "/620/SHARED.jpg") || states["SHARED#1"].UnlockTime != time.Date(2024, 11, 2, 15, 56, 0, 0, time.Local).Unix() || states["OTHER"].UnlockTime != time.Date(2016, 2, 11, 1, 11, 0, 0, time.Local).Unix() {
		t.Fatalf("legacy states = %#v, %v", states, err)
	}
}

func TestSteamProtectsCredentialsAndRejectsAmbiguousOwnedLibrary(t *testing.T) {
	for _, response := range []string{"redirect", `{"response":{}}`, `{"response":{"game_count":2,"games":[]}}`} {
		steam := steamTestClient(t, func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != "/IPlayerService/GetOwnedGames/v1/" {
				t.Error("credentialed redirect reached another route")
			}
			if response == "redirect" {
				http.Redirect(w, r, "https://other.invalid/leak?key=test-key", http.StatusTemporaryRedirect)
				return
			}
			fmt.Fprint(w, response)
		})
		owned, err := steam.Owned(context.Background(), steamTestOwner, Credentials{Key: "test-key"})
		if err == nil || strings.Contains(err.Error(), "test-key") || len(owned) != 0 {
			t.Fatalf("ambiguous owned data or unsafe error for %s = %#v, %v", response, owned, err)
		}
	}
}
