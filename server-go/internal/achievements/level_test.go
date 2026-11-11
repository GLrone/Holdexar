package achievements

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
	"time"

	"holdexar/server-go/internal/notifications"
	"holdexar/server-go/internal/store"
)

const levelTestSteamID = "76561198000000001"

func setupLevelTest(t *testing.T) (LevelService, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err := store.InitializeFreshDatabase(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	s := LevelService{
		DB:   db,
		Gate: store.NewWriteGate(db, path),
		Account: func(_ context.Context, target string) (string, bool, error) {
			if target != "" {
				t.Fatalf("level route tried to resolve explicit account %q", target)
			}
			return levelTestSteamID, true, nil
		},
		Now: func() time.Time { return time.Date(2026, 10, 10, 1, 2, 3, 456000000, time.UTC) },
	}
	return s, path
}

func levelTestExec(t *testing.T, db *sql.DB, query string, args ...any) {
	t.Helper()
	if _, err := db.Exec(query, args...); err != nil {
		t.Fatal(err)
	}
}

func levelTestGet(t *testing.T, server *httptest.Server, path string, out any) {
	t.Helper()
	resp, err := server.Client().Get(server.URL + "/api/v1" + path)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("GET %s = %d", path, resp.StatusCode)
	}
	if err := json.NewDecoder(resp.Body).Decode(out); err != nil {
		t.Fatal(err)
	}
}

func TestLevelBaselineReadThenSyncAwardsOnlyNewFacts(t *testing.T) {
	s, path := setupLevelTest(t)
	ctx := context.Background()
	oldUnlock := time.Date(2026, 10, 7, 12, 0, 0, 0, time.Local).Unix()
	newUnlock := time.Date(2026, 10, 8, 12, 0, 0, 0, time.Local).Unix()
	levelTestExec(t, s.DB, `INSERT INTO achievement_defs
		(appid,image_name,name,global_percent,display_order,updated_at)
		VALUES (10,'old','Old',5,0,'2026-10-10')`)
	levelTestExec(t, s.DB, `INSERT INTO achievement_games
		(steamid,appid,name,playtime_min,last_played,total_achievements,unlocked,source,updated_at)
		VALUES (?,10,'Ten',60,0,1,1,'owned','2026-10-10')`, levelTestSteamID)
	levelTestExec(t, s.DB, `INSERT INTO achievement_states
		(steamid,appid,image_name,achieved,unlock_time,synced_at)
		VALUES (?,10,'old',1,?,'2026-10-10')`, levelTestSteamID, oldUnlock)

	mux := http.NewServeMux()
	s.Register(mux)
	notifications.Register(mux, s.DB)
	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)
	var profile struct {
		SteamID                                     string
		Level, TotalXP, XPIntoLevel, XPForNextLevel int
		CurvePreview                                []struct{ Level, CostXP, CumulativeXP int }
		ActiveBonuses                               []struct {
			Kind       string
			Multiplier float64
			StreakDays int
		}
		RecentEvents []struct {
			Source    string
			XP        int
			CreatedAt string
		}
	}
	levelTestGet(t, server, "/achievements/level", &profile)
	if profile.SteamID != levelTestSteamID || profile.TotalXP != 303 || profile.Level != 4 ||
		profile.XPIntoLevel != 32 || profile.XPForNextLevel != 240 || len(profile.CurvePreview) != 6 ||
		profile.CurvePreview[0].CumulativeXP != 543 || len(profile.RecentEvents) != 5 ||
		profile.RecentEvents[4].Source != "baseline" || profile.RecentEvents[4].CreatedAt != "2026-10-10T01:02:03.456000" {
		t.Fatalf("baseline profile = %+v", profile)
	}
	if err := s.AwardSync(ctx, levelTestSteamID); err != nil {
		t.Fatal(err)
	}
	levelTestExec(t, s.DB, `UPDATE achievement_games SET playtime_min=300 WHERE steamid=?`, levelTestSteamID)
	levelTestExec(t, s.DB, `INSERT INTO achievement_games
		(steamid,appid,name,playtime_min,last_played,total_achievements,unlocked,source,updated_at)
		VALUES (?,20,'Twenty',0,0,1,1,'owned','2026-10-10')`, levelTestSteamID)
	levelTestExec(t, s.DB, `INSERT INTO achievement_states
		(steamid,appid,image_name,achieved,unlock_time,synced_at)
		VALUES (?,20,'new',1,?,'2026-10-10')`, levelTestSteamID, newUnlock)
	if err := s.AwardSync(ctx, levelTestSteamID); err != nil {
		t.Fatal(err)
	}

	reopened, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { reopened.Close() })
	s.DB, s.Gate = reopened, store.NewWriteGate(reopened, path)
	if err := s.AwardSync(ctx, levelTestSteamID); err != nil {
		t.Fatal(err)
	}
	levelTestGet(t, server, "/achievements/level?steamid="+levelTestSteamID, &profile)
	if profile.TotalXP != 606 || profile.Level != 5 || len(profile.RecentEvents) != 9 ||
		len(profile.ActiveBonuses) != 1 || profile.ActiveBonuses[0].Multiplier != 1.1 ||
		profile.ActiveBonuses[0].StreakDays != 2 {
		t.Fatalf("increment profile = %+v", profile)
	}
	var facts struct {
		Items []struct {
			Source, Kind string
			Data         struct {
				Count, AppID, FromLevel, ToLevel int
				Titles                           []string
			}
		}
	}
	levelTestGet(t, server, "/notifications/facts?afterId=0", &facts)
	if len(facts.Items) != 2 || facts.Items[0].Source != "achievements" ||
		facts.Items[0].Kind != "achievement_unlocked_batch" || facts.Items[0].Data.Count != 1 ||
		facts.Items[0].Data.AppID != 20 || len(facts.Items[0].Data.Titles) != 1 || facts.Items[0].Data.Titles[0] != "new" ||
		facts.Items[1].Kind != "level_up" || facts.Items[1].Data.FromLevel != 4 || facts.Items[1].Data.ToLevel != 5 {
		t.Fatalf("notification facts = %+v", facts)
	}
	levelTestGet(t, server, "/achievements/level?steamid=76561198000000002", &profile)
	if profile.SteamID != "76561198000000002" || profile.TotalXP != 0 || len(profile.RecentEvents) != 1 ||
		profile.RecentEvents[0].Source != "baseline" {
		t.Fatalf("other account = %+v", profile)
	}
}

func TestLevelWithoutAccountLeavesLedgerUntouched(t *testing.T) {
	s, _ := setupLevelTest(t)
	s.Account = nil
	mux := http.NewServeMux()
	s.Register(mux)
	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)
	var profile map[string]any
	levelTestGet(t, server, "/achievements/level?steamid=+", &profile)
	if profile["steamid"] != "" || profile["totalXp"] != float64(0) ||
		profile["nextTier"] != "silver" || profile["tierProgress"] != float64(0) ||
		len(profile["recentEvents"].([]any)) != 0 || len(profile["activeBonuses"].([]any)) != 0 {
		t.Fatalf("empty profile = %+v", profile)
	}
	var count int
	if err := s.DB.QueryRow("SELECT count(*) FROM xp_events").Scan(&count); err != nil || count != 0 {
		t.Fatalf("empty account wrote %d XP rows, err=%v", count, err)
	}
}

func TestLevelFirstReadAndSyncShareOneBatchedBaseline(t *testing.T) {
	s, _ := setupLevelTest(t)
	ctx := context.Background()
	levelTestExec(t, s.DB, `WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n<401)
		INSERT INTO achievement_states (steamid,appid,image_name,achieved,unlock_time,synced_at)
		SELECT ?,10,'achievement-'||n,1,0,'2026-10-10' FROM seq`, levelTestSteamID)

	started, syncDone := make(chan struct{}), make(chan error, 1)
	go func() {
		<-started
		syncDone <- s.AwardSync(ctx, levelTestSteamID)
	}()
	close(started)
	profile, err := s.profile(ctx, levelTestSteamID)
	syncErr := <-syncDone
	if err != nil || syncErr != nil {
		t.Fatalf("profile error = %v, sync error = %v", err, syncErr)
	}
	var markers, baselines int
	if err := s.DB.QueryRow(`SELECT count(*) FILTER (WHERE xp=0 AND note='baseline marker'),
		count(*) FILTER (WHERE source='baseline' AND source_ref='v1') FROM xp_events WHERE steamid=?`, levelTestSteamID).Scan(&markers, &baselines); err != nil {
		t.Fatal(err)
	}
	if profile.TotalXP != 401*15 || len(profile.RecentEvents) != 10 || markers != 401 || baselines != 1 {
		t.Fatalf("profile XP = %d, recent = %d, markers = %d, baselines = %d", profile.TotalXP, len(profile.RecentEvents), markers, baselines)
	}
}

func TestLevelSyncKeepsXPIfNoticeWriteFails(t *testing.T) {
	s, _ := setupLevelTest(t)
	ctx := context.Background()
	levelTestExec(t, s.DB, `INSERT INTO xp_events (steamid,source,source_ref,xp,multiplier,created_at)
		VALUES (?,'comment','10:old',20,1,'2026-10-06 00:00:00.000000')`, levelTestSteamID)
	levelTestExec(t, s.DB, `INSERT INTO achievement_states
		(steamid,appid,image_name,achieved,unlock_time,synced_at)
		VALUES (?,10,'new',1,0,'2026-10-10')`, levelTestSteamID)
	levelTestExec(t, s.DB, `CREATE TRIGGER fail_level_notices BEFORE INSERT ON fact_notices
		BEGIN SELECT RAISE(ABORT,'test notice failure'); END`)
	if err := s.AwardSync(ctx, levelTestSteamID); err == nil {
		t.Fatal("notice write failure was hidden")
	}
	var total, count int
	if err := s.DB.QueryRow("SELECT sum(xp),count(*) FROM xp_events WHERE steamid=?", levelTestSteamID).Scan(&total, &count); err != nil {
		t.Fatal(err)
	}
	if total != 35 || count != 2 {
		t.Fatalf("post-failure XP = %d in %d rows", total, count)
	}
}
