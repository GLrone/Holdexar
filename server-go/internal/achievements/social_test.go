package achievements

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func setupSocialTest(t *testing.T) (SocialService, *httptest.Server) {
	t.Helper()
	level, _ := setupLevelTest(t)
	social := SocialService{DB: level.DB, Gate: level.Gate, Now: level.Now,
		Account: func(context.Context, string) (string, bool, error) { return "primary", false, nil }}
	mux := http.NewServeMux()
	social.Register(mux)
	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)
	return social, server
}

func socialRequest(t *testing.T, server *httptest.Server, method, path, input string, wantStatus int) map[string]any {
	t.Helper()
	req, err := http.NewRequest(method, server.URL+"/api/v1/achievements"+path, strings.NewReader(input))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	res, err := server.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(res.Body).Decode(&out); err != nil {
		t.Fatalf("%s %s: %d %v", method, path, res.StatusCode, err)
	}
	if res.StatusCode != wantStatus {
		t.Fatalf("%s %s: status=%d want=%d response=%v", method, path, res.StatusCode, wantStatus, out)
	}
	return out
}

func TestSocialNotesHTTP(t *testing.T) {
	social, server := setupSocialTest(t)
	readTestExec(t, social.DB, `INSERT INTO achievement_defs(appid,image_name,name,icon_url,display_order,updated_at)
		VALUES (101,'note','Winner','https://example.test/icon',0,'2026-01-01')`)
	readTestExec(t, social.DB, `INSERT INTO xp_events(steamid,source,source_ref,xp,multiplier,created_at)
		VALUES ('primary','baseline','v1',19,1,'2026-01-01')`)
	readTestExec(t, social.DB, `INSERT INTO achievement_states(steamid,appid,image_name,achieved,unlock_time,synced_at) VALUES
		('primary',101,'note',1,?,'2026-01-01'),('primary',101,'second',1,?,'2026-01-01')`,
		time.Date(2026, 1, 2, 12, 0, 0, 0, time.Local).Unix(), time.Date(2026, 1, 3, 12, 0, 0, 0, time.Local).Unix())
	first := socialRequest(t, server, "POST", "/notes", `{"appid":101,"imageName":" note ","content":"  好\t  评  ","mood":"wow"}`, 200)
	readTestFields(t, first, map[string]any{"firstForAchievement": true, "xpGranted": float64(11)})
	readTestFields(t, first["note"].(map[string]any), map[string]any{"content": "好 评", "mood": "wow", "achievementName": "Winner", "achievementIcon": "https://example.test/icon", "achievementGrayIcon": nil, "createdAt": "2026-10-10T01:02:03.456000"})
	var count int
	if err := social.DB.QueryRow(`SELECT COUNT(*) FROM fact_notices WHERE kind='level_up' AND fact_key='primary:level:2'`).Scan(&count); err != nil || count != 1 {
		t.Fatalf("level up fact=%d, err=%v", count, err)
	}
	for i, method := range []string{"POST", "DELETE", "DELETE", "POST"} {
		if method == "POST" {
			out := socialRequest(t, server, method, "/notes", `{"appid":101,"imageName":"note","content":"edited"}`, 200)
			readTestFields(t, out, map[string]any{"firstForAchievement": false, "xpGranted": float64(0)})
		} else {
			out := socialRequest(t, server, method, "/notes/101/note", "", 200)
			readTestFields(t, out, map[string]any{"removed": i == 1})
		}
	}
	readTestFields(t, readTestGet(t, server, "/notes?steamid=other", 200), map[string]any{"total": float64(0), "notes": []any{}})
	readTestFields(t, readTestGet(t, server, "/notes", 200), map[string]any{"total": float64(1)})
	socialRequest(t, server, "POST", "/notes", `{"appid":101,"imageName":"note","content":"","mood":"wow"}`, 400)
	socialRequest(t, server, "POST", "/notes", `{"appid":101,"imageName":"note","content":"ok","mood":"no"}`, 400)
	socialRequest(t, server, "POST", "/notes", `{"appid":101,"imageName":"note"}`, 422)
	readTestExec(t, social.DB, `CREATE TRIGGER fail_social_xp BEFORE INSERT ON xp_events WHEN NEW.source_ref='101:rollback' BEGIN SELECT RAISE(ABORT,'fixture failure'); END;`)
	socialRequest(t, server, "POST", "/notes", `{"appid":101,"imageName":"rollback","content":"rollback"}`, 500)
	if err := social.DB.QueryRow(`SELECT COUNT(*) FROM achievement_notes WHERE image_name='rollback'`).Scan(&count); err != nil || count != 0 {
		t.Fatalf("failed award left note rows=%d, err=%v", count, err)
	}
}

func TestSocialCompareAndFeedHTTP(t *testing.T) {
	social, server := setupSocialTest(t)
	readTestExec(t, social.DB, `INSERT INTO achievement_defs(appid,image_name,name,global_percent,display_order,updated_at) VALUES
		(101,'rare','Rare',0.8,0,'2026-01-01'),(101,'common','Common',90,1,'2026-01-01')`)
	readTestExec(t, social.DB, `INSERT INTO achievement_games(steamid,appid,name,playtime_min,last_played,total_achievements,unlocked,source,updated_at)
		VALUES ('primary',101,'Game',600,0,1,1,'owned','2026-01-01')`)
	readTestExec(t, social.DB, `INSERT INTO achievement_states(steamid,appid,image_name,achieved,unlock_time,synced_at) VALUES
		('primary',101,'rare',1,101,'2026-01-01'),('primary',101,'common',1,0,'2026-01-01'),
		('primary',101,'orphan',1,102,'2026-01-01'),('primary',101,'locked',0,999,'2026-01-01'),
		('other',101,'common',1,900,'2026-01-01')`)
	readTestExec(t, social.DB, `INSERT INTO xp_events(steamid,source,source_ref,xp,multiplier,created_at) VALUES ('primary','comment','101:rare',40,1,'2026-01-01')`)
	readTestExec(t, social.DB, `INSERT INTO challenge_progress(steamid,challenge_id,progress,status,updated_at) VALUES
		('primary','one',1,'completed','2026-01-01'),('primary','two',1,'claimed','2026-01-01'),('primary','three',0,'active','2026-01-01')`)
	readTestExec(t, social.DB, `INSERT INTO steam_accounts(steam_id,cookies,persona_name,avatar_url,is_active,is_online,in_game,bound_at,wallet_error,wallet_backoff_level,wallet_fail_streak,wallet_frozen)
		VALUES ('primary','synthetic','Primary','https://example.test/avatar',1,0,'','2026-01-01','',0,0,0)`)
	rows := readTestGet(t, server, "/compare?steamids=primary,%20other,primary,third", 200)["accounts"].([]any)
	if len(rows) != 3 {
		t.Fatalf("unexpected rows %v", rows)
	}
	primary := rows[0].(map[string]any)
	readTestFields(t, primary, map[string]any{"steamid": "primary", "level": float64(2), "tier": "bronze", "totalXp": float64(40), "unlockedCount": float64(3), "platinumCount": float64(1), "playtimeMinutes": float64(600), "challengesCompleted": float64(2), "personaName": "Primary", "relation": "bound", "avatarUrl": "https://example.test/avatar"})
	readTestFields(t, primary["rarityBuckets"].([]any)[0].(map[string]any), map[string]any{"tier": "ultra", "count": float64(1)})
	readTestFields(t, primary["rarityBuckets"].([]any)[5].(map[string]any), map[string]any{"tier": "unknown", "count": float64(0)})
	readTestFields(t, rows[1].(map[string]any), map[string]any{"steamid": "other", "totalXp": float64(15), "unlockedCount": float64(1), "relation": ""})
	readTestFields(t, rows[2].(map[string]any), map[string]any{"steamid": "third", "totalXp": float64(0), "unlockedCount": float64(0)})
	rows = readTestGet(t, server, "/compare", 200)["accounts"].([]any)
	if len(rows) != 1 || rows[0].(map[string]any)["steamid"] != "primary" {
		t.Fatalf("unexpected default roster %v", rows)
	}
	rows = readTestGet(t, server, "/compare?steamids=1,2,3,4,5,6,7", 200)["accounts"].([]any)
	if len(rows) != 6 || rows[5].(map[string]any)["steamid"] != "6" {
		t.Fatalf("unexpected capped roster %v", rows)
	}
	readTestExec(t, social.DB, `DROP TABLE challenge_progress`)
	rows = readTestGet(t, server, "/compare", 200)["accounts"].([]any)
	readTestFields(t, rows[0].(map[string]any), map[string]any{"challengesCompleted": float64(0)})
	items := readTestGet(t, server, "/feed?limit=0", 200)["items"].([]any)
	if len(items) != 2 {
		t.Fatalf("unexpected feed %v", items)
	}
	readTestFields(t, items[0].(map[string]any), map[string]any{"imageName": "orphan", "name": "orphan", "rarity": "unknown", "gameName": "Game", "iconUrl": nil})
	readTestFields(t, items[1].(map[string]any), map[string]any{"imageName": "rare", "name": "Rare", "rarity": "ultra"})
	items = readTestGet(t, server, "/feed?steamid=other&limit=1", 200)["items"].([]any)
	readTestFields(t, items[0].(map[string]any), map[string]any{"imageName": "common", "gameName": ""})
	readTestGet(t, server, "/feed?limit=invalid", 422)
}
