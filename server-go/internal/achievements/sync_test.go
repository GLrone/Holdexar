package achievements

import (
	"context"
	"database/sql"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"holdexar/server-go/internal/jobrun"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/store"
)

func syncFixture(t *testing.T) (*sql.DB, *store.WriteGate) {
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
	return db, store.NewWriteGate(db, path)
}

func TestSyncRoutesAndDailyNoCredentialSkip(t *testing.T) {
	ctx := context.Background()
	db, gate := syncFixture(t)
	s := NewSync(ctx, db, gate, SyncOptions{})
	t.Cleanup(func() { s.Close(ctx) })
	mux := http.NewServeMux()
	s.Register(mux)
	start := time.Date(2026, 11, 8, 5, 19, 59, 0, sched.DefaultLoc)
	var events []sched.Event
	clock := sched.NewScheduler(sched.WithOnEvent(func(e sched.Event) { events = append(events, e) }))
	defer clock.Stop()
	if err := s.RegisterSchedules(ctx, clock); err != nil {
		t.Fatal(err)
	}
	clock.StartSim(start)
	clock.RunSim(start.Add(2 * time.Second))
	if len(events) != 1 || events[0].JobID != "achievements_sync" || events[0].Scheduled.Minute() != 20 {
		t.Fatalf("daily registration: %+v", events)
	}
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/v1/achievements/sync", nil))
	if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), `"running":false`) {
		t.Fatalf("empty status: %d %s", w.Code, w.Body.String())
	}
	w = httptest.NewRecorder()
	mux.ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/api/v1/achievements/sync?steamid=invalid", nil))
	if w.Code != http.StatusBadRequest {
		t.Fatalf("invalid owner: %d %s", w.Code, w.Body.String())
	}
}

type syncTestTransport struct {
	target *url.URL
	base   http.RoundTripper
}

func (t syncTestTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	req := r.Clone(r.Context())
	req.URL.Scheme, req.URL.Host = t.target.Scheme, t.target.Host
	return t.base.RoundTrip(req)
}

func TestManualAndDailyAchievementsSyncShareTablesAndDoNotDuplicateRequests(t *testing.T) {
	ctx := context.Background()
	db, gate := syncFixture(t)
	const a, b = "76561198000000001", "76561198000000002"
	if _, err := db.Exec(`INSERT INTO steam_accounts(steam_id,cookies,persona_name,avatar_url,is_active,is_online,in_game,bound_at,wallet_error,wallet_backoff_level,wallet_fail_streak,wallet_frozen)
		VALUES (?,'','Alpha','',0,0,'','2026-10-01','',0,0,0),(?,'','Beta','',1,0,'','2026-10-02','',0,0,0);
		INSERT INTO achievement_games(steamid,appid,name,playtime_min,last_played,total_achievements,unlocked,source,updated_at)
		VALUES (? ,999,'saved',33,0,0,0,'owned','2025-01-01'),(? ,555,'old shared',0,0,1,1,'shared','2025-01-01');`, a, b, a, b); err != nil {
		t.Fatal(err)
	}
	release, entered := make(chan struct{}), make(chan struct{})
	var aStates, bToken atomic.Int32
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		sid := r.FormValue("steamid")
		switch r.URL.Path {
		case "/IPlayerService/GetOwnedGames/v1/":
			if sid == a {
				fmt.Fprint(w, `{"response":{"game_count":1,"games":[{"appid":220,"name":"Alpha game","playtime_forever":111}]}}`)
			} else if sid == b && r.FormValue("access_token") == "beta-token" {
				bToken.Add(1)
				close(entered)
				select {
				case <-release:
				case <-r.Context().Done():
					return
				}
				fmt.Fprint(w, `{"response":{"game_count":1,"games":[{"appid":221,"name":"Beta game","playtime_forever":222}]}}`)
			} else {
				w.WriteHeader(http.StatusForbidden)
			}
		case "/IPlayerService/GetAchievementsProgress/v1/":
			if sid == a {
				w.WriteHeader(http.StatusUnauthorized)
			} else if sid == b && r.FormValue("access_token") == "beta-token" {
				fmt.Fprint(w, `{"response":{"achievement_progress":[{"appid":221,"total":1,"unlocked":1},{"appid":440,"total":1,"unlocked":1},{"appid":441,"total":1,"unlocked":0}]}}`)
			} else {
				w.WriteHeader(http.StatusUnauthorized)
			}
		case "/IPlayerService/GetGameAchievements/v1/":
			fmt.Fprint(w, `{"response":{"achievements":[{"icon":"fixture","icon_gray":"fixture_BW","internal_name":"GO_WIN","localized_name":"Fixture trophy","player_percent_unlocked":"4.2"}]}}`)
		case "/ISteamUserStats/GetPlayerAchievements/v1/":
			if sid == a {
				aStates.Add(1)
			}
			fmt.Fprintf(w, `{"playerstats":{"steamID":%q,"success":true,"achievements":[{"apiname":"GO_WIN","achieved":1,"unlocktime":1794065800}]}}`, sid)
		default:
			http.NotFound(w, r)
		}
	}))
	defer upstream.Close()
	target, _ := url.Parse(upstream.URL)
	client := upstream.Client()
	client.Transport = syncTestTransport{target, client.Transport}
	now := time.Date(2026, 11, 8, 5, 19, 59, 0, sched.DefaultLoc)
	level := LevelService{DB: db, Gate: gate, Now: func() time.Time { return now }}
	s := NewSync(ctx, db, gate, SyncOptions{
		Steam: NewSteam(SteamOptions{HTTPClient: client}), APIKey: func(context.Context) (string, error) { return "fixture-key", nil },
		Token: func(_ context.Context, sid string) (string, error) {
			if sid == b {
				return "beta-token", nil
			}
			return "alpha-token", nil
		},
		Shared: func(_ context.Context, sid string) (map[int64]string, bool, error) {
			if sid == b {
				return map[int64]string{440: "Shared unlocked", 441: "Shared locked"}, true, nil
			}
			return nil, false, nil
		},
		Recorder: jobrun.New(db, gate, time.Now, func(string, ...any) {}),
		Now:      func() time.Time { return now },
		Award:    level.AwardSync,
	})
	defer s.Close(ctx)
	_, f, err := s.start(ctx, b)
	if err != nil {
		t.Fatal(err)
	}
	select {
	case <-entered:
	case <-time.After(3 * time.Second):
		t.Fatal("selected owner request did not use its own token")
	}
	if _, _, err := s.start(ctx, a); err != errSyncBusy {
		t.Fatalf("duplicate job not rejected: %v", err)
	}
	close(release)
	select {
	case <-f.done:
	case <-time.After(3 * time.Second):
		t.Fatal("selected sync did not finish")
	}
	if f.err != nil {
		t.Fatal(f.err)
	}
	clock := sched.NewScheduler()
	defer clock.Stop()
	if err := s.RegisterSchedules(ctx, clock); err != nil {
		t.Fatal(err)
	}
	clock.StartSim(now)
	clock.RunSim(now.Add(2 * time.Second))
	status, err := s.Status(ctx)
	if err != nil || status.SteamID != a || status.Running || status.OK == nil || !*status.OK {
		t.Fatalf("unexpected scheduled result: %+v %v", status, err)
	}
	if aStates.Load() != 1 || bToken.Load() != 1 {
		t.Fatalf("duplicated or crossed fetches: A states=%d, B token=%d", aStates.Load(), bToken.Load())
	}
	assertSyncCount(t, db, `SELECT count(*) FROM achievement_games WHERE steamid=? AND appid IN (221,440) AND unlocked=1 AND state_synced_at IS NOT NULL`, 2, b)
	assertSyncCount(t, db, `SELECT count(*) FROM achievement_games WHERE steamid=? AND appid IN (441,555)`, 0, b)
	assertSyncCount(t, db, `SELECT count(*) FROM achievement_games WHERE steamid=? AND appid IN (220,999)`, 2, a)
	assertSyncCount(t, db, `SELECT count(*) FROM achievement_states WHERE steamid=? AND appid=220 AND achieved=1`, 1, a)
	assertSyncCount(t, db, `SELECT count(*) FROM xp_events WHERE steamid IN (?,?) AND xp>0`, 2, a, b)
	assertSyncCount(t, db, `SELECT count(*) FROM agent_runs WHERE status='done' AND json_extract(meta,'$.job')='achievements_sync'`, 1)
}

func assertSyncCount(t *testing.T, db *sql.DB, query string, want int, args ...any) {
	t.Helper()
	var got int
	if err := db.QueryRow(query, args...).Scan(&got); err != nil || got != want {
		t.Fatalf("%s: got=%d want=%d err=%v", query, got, want, err)
	}
}

func TestAchievementRecoveryAndBindingChangePreserveSavedFacts(t *testing.T) {
	ctx := context.Background()
	db, gate := syncFixture(t)
	const sid = "76561198000000001"
	if _, err := db.Exec(`INSERT INTO steam_accounts(steam_id,cookies,persona_name,avatar_url,is_active,is_online,in_game,bound_at,wallet_error,wallet_backoff_level,wallet_fail_streak,wallet_frozen)
		VALUES (?,'','Alpha','',1,0,'','2026-10-01','',0,0,0);
		INSERT INTO achievement_games(steamid,appid,name,playtime_min,last_played,total_achievements,unlocked,source,updated_at)
		VALUES (? ,220,'Saved game',111,0,1,1,'owned','2025-01-01');`, sid, sid); err != nil {
		t.Fatal(err)
	}
	s := NewSync(ctx, db, gate, SyncOptions{})
	defer s.Close(ctx)
	owner, err := s.owner(ctx, sid)
	if err != nil || !owner.bound {
		t.Fatalf("failed to capture owner: %+v %v", owner, err)
	}
	stale := Snapshot{Running: true, Stage: "details", SteamID: sid, Done: 5, Total: 6, Synced: "2026-11-07T07:11:00Z"}
	if err := s.saveSnapshot(ctx, stale); err != nil {
		t.Fatal(err)
	}
	if err := s.Recover(ctx); err != nil {
		t.Fatal(err)
	}
	status, err := s.Status(ctx)
	if err != nil || status.Running || status.OK == nil || *status.OK || status.Stage != "" || status.Synced != stale.Synced || status.Done != 5 {
		t.Fatalf("stale progress not recovered: %+v %v", status, err)
	}
	if _, err := db.Exec(`UPDATE steam_accounts SET bound_at='2026-11-01' WHERE steam_id=?`, sid); err != nil {
		t.Fatal(err)
	}
	if err := s.writeOwned(ctx, owner, []OwnedGame{{AppID: 220, Name: "Late data", Playtime: 222}}); err != errOwnerChanged {
		t.Fatalf("old binding wrote data: %v", err)
	}
	assertSyncCount(t, db, `SELECT count(*) FROM achievement_games WHERE steamid=? AND appid=220 AND playtime_min=111 AND unlocked=1`, 1, sid)
}
