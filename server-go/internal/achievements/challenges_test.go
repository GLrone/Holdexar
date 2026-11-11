package achievements

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"holdexar/server-go/internal/notifications"
)

func setupChallengesTest(t *testing.T) (ChallengeService, *httptest.Server) {
	t.Helper()
	level, _ := setupLevelTest(t)
	s := ChallengeService{DB: level.DB, Gate: level.Gate, Account: level.Account, Now: level.Now}
	mux := http.NewServeMux()
	s.Register(mux)
	notifications.Register(mux, s.DB)
	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)
	return s, server
}

func challengePost(t *testing.T, server *httptest.Server, id string, result any) (int, error) {
	t.Helper()
	res, err := server.Client().Post(server.URL+"/api/v1/achievements/challenges/"+id+"/claim", "application/json", nil)
	if err != nil {
		return 0, err
	}
	defer res.Body.Close()
	return res.StatusCode, json.NewDecoder(res.Body).Decode(result)
}

func TestChallengesOverviewMeasuresExistingFactsAndKeepsEarnedStatus(t *testing.T) {
	s, server := setupChallengesTest(t)
	ctx := context.Background()
	localDate := time.Date(2026, 10, 7, 12, 0, 0, 0, time.Local)
	levelTestExec(t, s.DB, `INSERT INTO achievement_states
		(steamid,appid,image_name,achieved,unlock_time,synced_at)
		VALUES (?,10,'first',1,?,'2026-10-10'),(?,10,'second',1,?,'2026-10-10'),(?,20,'third',1,?,'2026-10-10'),
		(?,20,'locked',0,0,'2026-10-10')`, levelTestSteamID, localDate.Unix(),
		levelTestSteamID, localDate.AddDate(0, 0, 1).Unix(), levelTestSteamID, localDate.AddDate(0, 0, 2).Unix(), levelTestSteamID)
	levelTestExec(t, s.DB, `INSERT INTO achievement_games
		(steamid,appid,name,playtime_min,last_played,total_achievements,unlocked,source,updated_at)
		VALUES (?,10,'Ten',500,0,2,2,'owned','2026-10-10'),(?,20,'Twenty',59,0,2,1,'owned','2026-10-10')`,
		levelTestSteamID, levelTestSteamID)
	levelTestExec(t, s.DB, `INSERT INTO achievement_notes
		(steamid,appid,image_name,content,created_at,updated_at) VALUES (?,10,'first','hello','2026-10-10','2026-10-10')`, levelTestSteamID)
	levelTestExec(t, s.DB, `INSERT INTO family_groups (steamid,members_json,fetch_ok,member_count,updated_at)
		VALUES (?,'[{"steamid":"76561198000000001"},{"steamid":"76561198000000002"}]',1,2,'2026-10-10')`, levelTestSteamID)
	levelTestExec(t, s.DB, `WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n<10)
		INSERT INTO achievement_states (steamid,appid,image_name,achieved,unlock_time,synced_at)
		SELECT '76561198000000002',10,'member-'||n,1,0,'2026-10-10' FROM seq`)
	if err := s.Recompute(ctx, levelTestSteamID); err != nil {
		t.Fatal(err)
	}
	var profile challengeOverview
	levelTestGet(t, server, "/achievements/challenges", &profile)
	if profile.SteamID != levelTestSteamID || profile.Summary.Total != 20 || profile.Summary.Completed != 6 ||
		profile.Summary.Claimed != 0 || profile.Summary.Claimable != 6 {
		t.Fatalf("overview = %+v", profile)
	}
	items := make(map[string]challengeItem)
	for _, item := range profile.Challenges {
		items[item.ID] = item
	}
	for id, want := range map[string]int{
		"first_trophy": 3, "platinum_first": 1, "full_clear_first": 1, "breadth_twenty": 2,
		"hours_hundred": 559, "buckets_ten": 4, "streak_three": 3, "note_first": 1, "social_duo": 2, "social_trio": 1,
	} {
		if items[id].Progress != want {
			t.Fatalf("%s progress = %d, want %d", id, items[id].Progress, want)
		}
	}
	if items["first_trophy"].CompletedAt == nil || *items["first_trophy"].CompletedAt != "2026-10-10T01:02:03.456000" {
		t.Fatalf("completion time = %+v", items["first_trophy"])
	}
	levelTestExec(t, s.DB, "DELETE FROM achievement_states WHERE steamid=?", levelTestSteamID)
	levelTestGet(t, server, "/achievements/challenges", &profile)
	first := profile.Challenges[0]
	if first.Progress != 0 || first.Status != "completed" || !first.Claimable || first.CompletedAt == nil ||
		*first.CompletedAt != *items["first_trophy"].CompletedAt {
		t.Fatalf("earned status after facts change = %+v", first)
	}
	var facts struct {
		Items []struct{ Kind string }
	}
	levelTestGet(t, server, "/notifications/facts?afterId=0", &facts)
	if len(facts.Items) != 6 {
		t.Fatalf("completion notices = %v", facts.Items)
	}
	for _, fact := range facts.Items {
		if fact.Kind != "challenge_completed" {
			t.Fatalf("unexpected notice %+v", fact)
		}
	}
}

func TestChallengesClaimCommitsRewardOnceAndRollsBackOnWriteFailure(t *testing.T) {
	s, server := setupChallengesTest(t)
	levelTestExec(t, s.DB, `INSERT INTO achievement_states
		(steamid,appid,image_name,achieved,unlock_time,synced_at)
		VALUES (?,10,'first',1,0,'2026-10-10')`, levelTestSteamID)
	levelTestExec(t, s.DB, `CREATE TRIGGER fail_challenge_claim BEFORE UPDATE ON challenge_progress
		WHEN NEW.status='claimed' BEGIN SELECT RAISE(ABORT,'test claim failure'); END`)

	var body map[string]any
	if status, err := challengePost(t, server, "first_trophy", &body); err != nil || status != http.StatusInternalServerError {
		t.Fatalf("failing claim status=%d body=%v error=%v", status, body, err)
	}
	var count int
	if err := s.DB.QueryRow("SELECT count(*) FROM xp_events WHERE source='challenge_claim'").Scan(&count); err != nil || count != 0 {
		t.Fatalf("reward persisted through a failing status write, count=%d err=%v", count, err)
	}
	levelTestExec(t, s.DB, "DROP TRIGGER fail_challenge_claim")

	type claimAttempt struct {
		status int
		result challengeClaim
		err    error
	}
	attempts := make(chan claimAttempt, 2)
	var wg sync.WaitGroup
	for range 2 {
		wg.Go(func() {
			var result challengeClaim
			status, err := challengePost(t, server, "first_trophy", &result)
			attempts <- claimAttempt{status, result, err}
		})
	}
	wg.Wait()
	close(attempts)
	statuses := map[int]int{}
	for attempt := range attempts {
		if attempt.err != nil {
			t.Fatal(attempt.err)
		}
		statuses[attempt.status]++
		if attempt.status == 200 && (attempt.result.XPGranted != 80 || attempt.result.TotalXP != 80 ||
			attempt.result.Level.Previous != 1 || attempt.result.Level.Current != 2 || !attempt.result.Level.LeveledUp) {
			t.Fatalf("winning claim = %+v", attempt.result)
		}
	}
	if statuses[http.StatusOK] != 1 || statuses[http.StatusConflict] != 1 {
		t.Fatalf("concurrent claim statuses = %v", statuses)
	}
	if err := s.DB.QueryRow("SELECT count(*) FROM xp_events WHERE source='challenge_claim' AND xp=80").Scan(&count); err != nil || count != 1 {
		t.Fatalf("reward count=%d err=%v", count, err)
	}
	if err := s.DB.QueryRow("SELECT count(*) FROM fact_notices WHERE kind='level_up'").Scan(&count); err != nil || count != 1 {
		t.Fatalf("level-up notice count=%d err=%v", count, err)
	}
	var overview challengeOverview
	levelTestGet(t, server, "/achievements/challenges", &overview)
	first := overview.Challenges[0]
	if first.Status != "claimed" || first.Claimable || first.ClaimedAt == nil || first.CompletedAt == nil ||
		overview.Summary.Completed != 1 || overview.Summary.Claimed != 1 || overview.Summary.Claimable != 0 {
		t.Fatalf("claimed challenge after recompute = %+v, summary = %+v", first, overview.Summary)
	}
	if status, err := challengePost(t, server, "streak_three", &body); err != nil || status != http.StatusConflict ||
		!strings.Contains(body["detail"].(string), "尚未完成") {
		t.Fatalf("incomplete challenge status=%d body=%v err=%v", status, body, err)
	}
	if status, err := challengePost(t, server, "missing", &body); err != nil || status != http.StatusNotFound {
		t.Fatalf("missing challenge status=%d body=%v err=%v", status, body, err)
	}
}

func TestChallengesExistingXPBlocksAnotherClaimAndIdleLeavesNoRows(t *testing.T) {
	s, server := setupChallengesTest(t)
	levelTestExec(t, s.DB, `INSERT INTO achievement_states
		(steamid,appid,image_name,achieved,unlock_time,synced_at)
		VALUES (?,10,'first',1,0,'2026-10-10')`, levelTestSteamID)
	levelTestExec(t, s.DB, `INSERT INTO xp_events (steamid,source,source_ref,xp,multiplier,created_at)
		VALUES (?,'challenge_claim','first_trophy',80,1,'2026-10-10 01:02:03')`, levelTestSteamID)
	var body map[string]any
	if status, err := challengePost(t, server, "first_trophy", &body); err != nil || status != http.StatusConflict {
		t.Fatalf("prior reward status=%d body=%v err=%v", status, body, err)
	}
	var status string
	var count int
	if err := s.DB.QueryRow(`SELECT status,count(*) FROM challenge_progress JOIN xp_events USING (steamid)
		WHERE challenge_id='first_trophy' AND claimed_at IS NULL AND xp_events.source='challenge_claim'`).Scan(&status, &count); err != nil ||
		status != "completed" || count != 1 {
		t.Fatalf("persisted status = %q, rewards = %d, err = %v", status, count, err)
	}

	s.Account = nil
	overview, err := s.overview(context.Background(), "")
	if err != nil || overview.SteamID != "" || overview.Summary.Total != 20 || overview.Summary.Completed != 0 ||
		overview.Challenges[0].UpdatedAt != nil {
		t.Fatalf("idle overview = %+v, err = %v", overview, err)
	}
	if err := s.DB.QueryRow("SELECT count(*) FROM challenge_progress WHERE steamid=''").Scan(&count); err != nil || count != 0 {
		t.Fatalf("idle overview wrote %d rows, err=%v", count, err)
	}
}
