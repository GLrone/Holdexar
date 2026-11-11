package achievements

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestCareerHTTPProjectsSelectedSnapshot(t *testing.T) {
	db := readTestDB(t)
	readTestExec(t, db, `INSERT INTO achievement_games(steamid,appid,name,playtime_min,last_played,total_achievements,unlocked,source,updated_at) VALUES
		('selected',620,'传送门 2',900,10,6,6,'owned','2026-01-01'),
		('selected',621,'传送门',60,20,10,5,'','2026-01-01'),
		('selected',440,'Shared',0,0,1,1,'shared','2026-01-01'),
		('other',620,'Wrong account name',99,99,6,1,'owned','2026-01-01')`)
	readTestExec(t, db, `INSERT INTO games(appid,name,series_id,min_cny_fen,positive_rate,release_date,chinese_support,developers,publishers,
		family_sharing,trading_cards,is_epic,is_hb,hl_flag,pp_flag,diff_fen,is_adult,is_visual_novel,removed_strikes,positive_reviews,review_count,view_count) VALUES
		(620,'传送门 2','Portal',3700,9800,'2011-04-18','full','["Valve","Partner"]','["Valve"]',0,0,0,0,0,0,0,0,0,0,0,0,0),
		(621,'传送门','Portal',NULL,9000,'2007-10-10',NULL,'["Valve"]','["Valve"]',0,0,0,0,0,0,0,0,0,0,0,0,0)`)
	readTestExec(t, db, `INSERT INTO tags(tagid,name_zh) VALUES (501,'动作'),(502,'冒险'),(503,NULL)`)
	readTestExec(t, db, `INSERT INTO game_tags(appid,tagid,weight) VALUES (620,501,3),(620,502,1),(620,503,100),(621,501,1)`)
	readTestExec(t, db, `INSERT INTO achievement_defs(appid,image_name,name,description,global_percent,display_order,updated_at) VALUES
		(620,'a','First','one<br/>long description',0.5,0,'2026-01-01'),
		(620,'b','Second','one long description',3,1,'2026-01-01'),
		(620,'c','Third','short',6,2,'2026-01-01'),
		(620,'d','Fourth','fourth description',60,3,'2026-01-01'),
		(620,'e','Fifth',NULL,40,4,'2026-01-01'),
		(620,'f','Sixth',NULL,80,5,'2026-01-01'),
		(440,'a','Shared',NULL,NULL,0,'2026-01-01')`)
	first := time.Date(2024, time.January, 1, 3, 0, 0, 0, time.Local).Unix()
	readTestExec(t, db, `INSERT INTO achievement_states(steamid,appid,image_name,achieved,unlock_time,synced_at) VALUES
		('selected',620,'a',1,?,'2026-01-01'),('selected',620,'b',1,?,'2026-01-01'),
		('selected',620,'c',1,?,'2026-01-01'),('selected',620,'d',1,?,'2026-01-01'),
		('selected',620,'e',1,?,'2026-01-01'),('selected',620,'f',1,?,'2026-01-01'),
		('selected',440,'a',1,0,'2026-01-01'),('other',620,'a',1,?,'2026-01-01')`,
		first, first+3600, first+7200, first+10800, first+14400, first+86400, first-3600)
	mux := http.NewServeMux()
	Reader{DB: db, Account: func(_ context.Context, sid string) (string, bool, error) { return sid, sid != "", nil },
		Snapshot: func(context.Context) (string, string, bool, error) { return "other", "2026-01-02", false, nil }}.RegisterCareer(mux)
	server := httptest.NewServer(mux)
	defer server.Close()
	body := readTestGet(t, server, "/career?steamid=selected", 200)
	readTestFields(t, body, map[string]any{"steamid": "selected", "hasCredential": true, "lastSyncedAt": nil, "unfinishedCount": float64(1), "completedGames": float64(2)})
	readTestFields(t, body["playtime"].(map[string]any), map[string]any{"totalMin": float64(960), "playedGames": float64(2), "histogram": []any{float64(0), float64(0), float64(1), float64(1), float64(0), float64(0)}})
	readTestFields(t, body["trophy"].(map[string]any), map[string]any{"total": float64(17), "unlocked": float64(12), "rareCount": float64(2), "rareShare": 28.6, "rate": 70.6})
	readTestFields(t, body["activity"].(map[string]any), map[string]any{"firstDate": "2024-01-01", "lastDate": "2024-01-02", "longestStreak": float64(2), "maxGapDays": float64(1), "totalUnlocks": float64(6)})
	if len(body["activity"].(map[string]any)["hourWeekday"].([]any)) != 168 {
		t.Fatal("invalid activity heatmap")
	}
	readTestFields(t, body["library"].(map[string]any), map[string]any{"valueFen": float64(3700), "costPerHourFen": 231.2, "avgPositiveRate": float64(94)})
	series := body["series"].(map[string]any)["rows"].([]any)
	readTestFields(t, series[0].(map[string]any), map[string]any{"name": "传送门", "owned": float64(2), "progress": 0.688, "playedUnlocked": float64(11)})
	readTestFields(t, body["taste"].(map[string]any)["tags"].([]any)[0].(map[string]any), map[string]any{"tag": "动作", "playtimeMin": float64(735)})
	readTestFields(t, body["records"].(map[string]any)["fastestComplete"].(map[string]any), map[string]any{"spanMin": float64(1440), "unlocks": float64(6)})
	quotes := body["quotes"].([]any)
	if len(quotes) != 2 {
		t.Fatalf("quotes=%#v", quotes)
	}
	readTestFields(t, quotes[0].(map[string]any), map[string]any{"name": "First", "text": "one long description", "gameName": "传送门 2", "globalPercent": 0.5})
	if len(body["milestones"].([]any)) != 2 || len(body["spotlight"].([]any)) != 2 {
		t.Fatal("unexpected milestones or spotlight count")
	}
	empty := readTestGet(t, server, "/career", 200)
	readTestFields(t, empty, map[string]any{"steamid": "", "lastSyncedAt": nil, "yearly": []any{}, "quotes": []any{}, "spotlight": []any{}})
	readTestFields(t, empty["activity"].(map[string]any), map[string]any{"hourWeekday": []any{}, "monthly": []any{}, "busiestDay": nil, "days": map[string]any{}})
}
