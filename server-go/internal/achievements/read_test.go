package achievements

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"reflect"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
)

func readTestDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := store.Open(filepath.Join(t.TempDir(), "achievements.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err := store.InitializeFreshDatabase(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	return db
}

func readTestExec(t *testing.T, db *sql.DB, query string, args ...any) {
	t.Helper()
	if _, err := db.Exec(query, args...); err != nil {
		t.Fatal(err)
	}
}

func readTestGet(t *testing.T, server *httptest.Server, path string, wantStatus int) map[string]any {
	t.Helper()
	res, err := server.Client().Get(server.URL + "/api/v1/achievements" + path)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	var body map[string]any
	if err := json.NewDecoder(res.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if res.StatusCode != wantStatus {
		t.Fatalf("GET %s: status=%d, want %d; body=%v", path, res.StatusCode, wantStatus, body)
	}
	return body
}

func readTestFields(t *testing.T, actual map[string]any, want map[string]any) {
	t.Helper()
	for key, value := range want {
		if got, ok := actual[key]; !ok || !reflect.DeepEqual(got, value) {
			t.Errorf("%s = %#v, want %#v", key, got, value)
		}
	}
}

func TestReaderBasicHTTP(t *testing.T) {
	db := readTestDB(t)
	now := time.Now()
	last := now.Unix() - 60
	readTestExec(t, db, `INSERT INTO achievement_games(steamid,appid,name,playtime_min,last_played,total_achievements,unlocked,source,updated_at) VALUES
		('primary',101,'Bravo',90,100,3,3,'','2026-01-01'),
		('primary',202,'Älpha',10,300,4,3,'shared','2026-01-01'),
		('primary',303,'Charlie',15,200,0,0,'manual','2026-01-01'),
		('other',101,'Other',99,400,3,1,'owned','2026-01-01'),
		('other',404,NULL,1,500,0,0,'owned','2026-01-01')`)
	readTestExec(t, db, `INSERT INTO games(appid,name,header_image,family_sharing,trading_cards,is_epic,is_hb,hl_flag,pp_flag,diff_fen,is_adult,is_visual_novel,removed_strikes,positive_reviews,review_count,view_count)
		VALUES (101,'Main catalogue','https://example.test/header',0,0,0,0,0,0,0,0,0,0,0,0,0)`)
	readTestExec(t, db, `INSERT INTO achievement_defs(appid,image_name,name,description,icon_url,icon_gray_url,global_percent,display_order,updated_at) VALUES
		(101,'rare','Rare',NULL,'https://example.test/rare',NULL,0.5,2,'2026-01-01'),
		(101,'unknown',NULL,'Hidden',NULL,NULL,NULL,1,'2026-01-01'),
		(101,'common','Common',NULL,NULL,NULL,70,0,'2026-01-01'),
		(404,'new','New',NULL,NULL,NULL,12,0,'2026-01-01')`)
	readTestExec(t, db, `INSERT INTO achievement_states(steamid,appid,image_name,achieved,unlock_time,synced_at) VALUES
		('primary',101,'rare',1,?,'2026-01-01'),
		('primary',101,'unknown',1,0,'2026-01-01'),
		('primary',101,'common',1,?,'2026-01-01'),
		('primary',101,'orphan',1,?,'2026-01-01'),
		('other',101,'common',1,?,'2026-01-01')`, last, last-10, last+10, last+20)

	stamp := now.Add(-7 * time.Hour).Format(time.RFC3339)
	warmed := make(chan string, 4)
	mux := http.NewServeMux()
	Reader{
		DB: db,
		Account: func(_ context.Context, sid string) (string, bool, error) {
			if sid == "" {
				sid = "primary"
			}
			return sid, sid == "primary", nil
		},
		Snapshot: func(context.Context) (string, string, bool, error) { return "", stamp, false, nil },
		Warm:     func(sid string) { warmed <- sid },
	}.Register(mux)
	server := httptest.NewServer(mux)
	defer server.Close()

	summary := readTestGet(t, server, "/summary", 200)
	readTestFields(t, summary, map[string]any{
		"steamid": "primary", "hasCredential": true, "lastSyncedAt": stamp, "platinum": float64(1),
		"gamesWithAchievements": float64(2), "playedGames": float64(3), "totalAchievements": float64(7),
		"unlockedAchievements": float64(6), "completionRate": 85.7, "totalPlaytimeMin": float64(115),
		"externalGames": float64(1), "externalUnlocked": float64(3), "externalPlatinum": float64(0),
	})
	readTestFields(t, summary["platinums"].([]any)[0].(map[string]any), map[string]any{"appid": float64(101), "date": float64(last), "source": "owned", "headerImage": "https://example.test/header"})
	readTestFields(t, summary["nearCompletion"].([]any)[0].(map[string]any), map[string]any{"appid": float64(202), "remaining": float64(1), "source": "shared"})
	readTestFields(t, summary["rarest"].([]any)[0].(map[string]any), map[string]any{"imageName": "rare", "gameName": "Bravo", "globalPercent": 0.5})
	if timeline := summary["unlockTimeline"].([]any); len(timeline) != 24 || timeline[23].(map[string]any)["month"] != now.Format("2006-01") {
		t.Fatalf("unexpected timeline: %v", timeline)
	}
	select {
	case sid := <-warmed:
		if sid != "primary" {
			t.Fatalf("warmed %s", sid)
		}
	default:
		t.Fatal("stale summary did not enqueue its own account")
	}
	other := readTestGet(t, server, "/summary?steamid=%20other%20", 200)
	readTestFields(t, other, map[string]any{"steamid": "other", "hasCredential": false, "unlockedAchievements": float64(1)})
	select {
	case sid := <-warmed:
		t.Fatalf("unexpected warm for %s", sid)
	default:
	}

	for _, test := range []struct {
		query string
		ids   []float64
	}{
		{"", []float64{101, 202}},
		{"?filter=invalid&sort=invalid", []float64{101, 202}},
		{"?filter=all&sort=recent", []float64{202, 303, 101}},
		{"?filter=platinum", []float64{101}},
		{"?filter=progress&sort=progress", []float64{202}},
		{"?filter=external&sort=name&q=%20%C3%84L%20", []float64{202}},
		{"?filter=all&steamid=other", []float64{101, 404}},
	} {
		list := readTestGet(t, server, "/games"+test.query, 200)
		ids := []float64{}
		for _, row := range list["games"].([]any) {
			ids = append(ids, row.(map[string]any)["appid"].(float64))
		}
		if !reflect.DeepEqual(ids, test.ids) || list["count"] != float64(len(ids)) {
			t.Errorf("%s: ids=%v count=%v, want %v", test.query, ids, list["count"], test.ids)
		}
	}
	first := readTestGet(t, server, "/games/101", 200)
	readTestFields(t, first, map[string]any{"perfectDate": float64(last), "platinum": true, "owned": true})
	if _, exists := first["remaining"]; exists {
		t.Error("list-only field remaining in detail")
	}
	otherDetail := readTestGet(t, server, "/games/101?steamid=other", 200)
	rows := otherDetail["achievements"].([]any)
	readTestFields(t, rows[0].(map[string]any), map[string]any{"imageName": "common", "achieved": true})
	readTestFields(t, rows[1].(map[string]any), map[string]any{"imageName": "rare", "achieved": false, "rarity": "ultra", "iconGray": nil})
	readTestFields(t, rows[2].(map[string]any), map[string]any{"imageName": "unknown", "name": "unknown", "achieved": false, "rarity": "unknown", "globalPercent": nil})
	legacy := readTestGet(t, server, "/games/404?steamid=other", 200)
	readTestFields(t, legacy, map[string]any{"total": float64(1), "name": "", "headerImage": "https://cdn.cloudflare.steamstatic.com/steam/apps/404/header.jpg"})
	readTestFields(t, readTestGet(t, server, "/games/404", 404), map[string]any{"detail": "该游戏不在成就快照中，请先同步"})
	readTestGet(t, server, "/games/1_01.0", 200)
	readTestGet(t, server, "/games/invalid", 422)
}

func TestReaderAccountsAndEmptyHTTP(t *testing.T) {
	db := readTestDB(t)
	readTestExec(t, db, `INSERT INTO steam_accounts(steam_id,cookies,persona_name,avatar_url,is_active,is_online,in_game,bound_at,wallet_error,wallet_backoff_level,wallet_fail_streak,wallet_frozen) VALUES
		('primary','secret','Primary','',0,0,'','2026-01-01','',0,0,0),
		('other','secret','Other','',1,0,'','2026-02-01','',0,0,0)`)
	readTestExec(t, db, `INSERT INTO family_groups(steamid,members_json,fetch_ok,member_count,updated_at) VALUES
		('primary','[{"steamid":"primary","personaName":"family duplicate"},{"steamid":"legacy","persona_name":"Legacy","avatar_url":"old"},{"steamid":123},{"steamid":"camel","personaName":"Camel","avatarUrl":"new"}]',1,4,'2026-01-01'),
		('other','[{"steamid":"wrong-family"}]',1,1,'2026-01-01')`)
	readTestExec(t, db, `INSERT INTO tracked_accounts(steamid,persona_name,avatar_url) VALUES
		('123','Tracked','tracked-avatar'),('primary','Unused','primary-avatar'),('unrelated','Unrelated','')`)
	mux := http.NewServeMux()
	Reader{DB: db, Account: func(context.Context, string) (string, bool, error) { return "", false, nil }}.Register(mux)
	server := httptest.NewServer(mux)
	defer server.Close()
	accounts := readTestGet(t, server, "/accounts", 200)["accounts"].([]any)
	if len(accounts) != 5 {
		t.Fatalf("accounts = %v", accounts)
	}
	for i, want := range []map[string]any{
		{"steamid": "primary", "personaName": "Primary", "avatarUrl": "primary-avatar", "isPrimary": true, "isActive": false, "relation": "bound"},
		{"steamid": "other", "personaName": "Other", "avatarUrl": "", "isPrimary": false, "isActive": true, "relation": "bound"},
		{"steamid": "camel", "personaName": "Camel", "avatarUrl": "new", "isPrimary": false, "isActive": false, "relation": "family"},
		{"steamid": "legacy", "personaName": "Legacy", "avatarUrl": "old", "isPrimary": false, "isActive": false, "relation": "family"},
		{"steamid": "123", "personaName": "Tracked", "avatarUrl": "tracked-avatar", "isPrimary": false, "isActive": false, "relation": "family"},
	} {
		row := accounts[i].(map[string]any)
		readTestFields(t, row, want)
		if len(row) != len(want) {
			t.Errorf("unexpected account fields: %v", row)
		}
	}
	readTestFields(t, readTestGet(t, server, "/summary", 200), map[string]any{
		"steamid": "", "hasCredential": false, "lastSyncedAt": nil, "rarityBuckets": []any{}, "unlockTimeline": []any{},
	})
	readTestFields(t, readTestGet(t, server, "/games", 200), map[string]any{"games": []any{}, "count": float64(0)})
	readTestGet(t, server, "/games/101", 404)
}

func TestReaderWarmScopeHTTP(t *testing.T) {
	db := readTestDB(t)
	stamp := time.Now().Add(-7 * time.Hour).Format(time.RFC3339Nano)
	for _, tc := range []struct {
		name, owner, synced string
		running, credential bool
		visible, warms      bool
	}{
		{"same", "primary", stamp, false, true, true, true},
		{"other", "other", stamp, false, true, false, false},
		{"running", "primary", stamp, true, true, true, false},
		{"no credential", "primary", stamp, false, false, true, false},
		{"never synced", "primary", "", false, true, false, false},
		{"fresh", "primary", time.Now().Format(time.RFC3339Nano), false, true, true, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			warmed := make(chan string, 1)
			mux := http.NewServeMux()
			Reader{
				DB:       db,
				Account:  func(context.Context, string) (string, bool, error) { return "primary", tc.credential, nil },
				Snapshot: func(context.Context) (string, string, bool, error) { return tc.owner, tc.synced, tc.running, nil },
				Warm:     func(sid string) { warmed <- sid },
			}.Register(mux)
			server := httptest.NewServer(mux)
			defer server.Close()
			summary := readTestGet(t, server, "/summary", 200)
			var synced any
			if tc.visible {
				synced = tc.synced
			}
			readTestFields(t, summary, map[string]any{"lastSyncedAt": synced})
			select {
			case sid := <-warmed:
				if sid != "primary" || !tc.warms {
					t.Errorf("unexpected warm for %s", sid)
				}
			default:
				if tc.warms {
					t.Error("no warm for stale own snapshot")
				}
			}
		})
	}
}
