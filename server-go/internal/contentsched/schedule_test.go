package contentsched

import (
	"context"
	"database/sql"
	"errors"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"holdexar/server-go/internal/jobrun"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
)

func epicScheduleFixture(t *testing.T) (*sql.DB, *store.WriteGate, *Automation) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err := store.InitializeFreshDatabase(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	gate := store.NewWriteGate(db, path)
	automation := &Automation{Job: "epic_free", Label: "Epic 免费游戏", Field: "epic_schedule", Key: settings.EpicFreeScheduleKey, DB: db, Log: t.Logf, Now: time.Now}
	automation.Recorder = jobrun.New(db, gate, func() time.Time { return automation.Now() }, t.Logf)
	return db, gate, automation
}

func TestBundleStartupThenDailyCadenceAndSwitch(t *testing.T) {
	db, gate, automation := epicScheduleFixture(t)
	now := epicScheduleTime(t, "2026-11-09 14:00")
	automation.Job, automation.Source, automation.Field, automation.Key = "bartervg_bundles", "bundle_counts", "", ""
	automation.Daily = sched.DailyTrigger{Hour: 5, Minute: 40}
	automation.FirstAfter = 90 * time.Second
	automation.Now = func() time.Time { return now }
	calls := 0
	automation.Run = func(context.Context) (map[string]any, error) { calls++; return nil, nil }
	clock := sched.NewScheduler()
	defer clock.Stop()
	if err := automation.Register(t.Context(), clock); err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	settings.Register(mux, db, gate, settings.Options{Changed: automation.SettingsChanged})
	putEpicSchedule(t, mux, `{"bundle_counts":false}`)
	clock.StartSim(now)
	clock.RunSim(now.Add(89 * time.Second))
	var runs int
	if err := db.QueryRow(`SELECT count(*) FROM agent_runs`).Scan(&runs); err != nil || runs != 0 {
		t.Fatalf("before startup tick: runs=%d err=%v", runs, err)
	}
	now = now.Add(90 * time.Second)
	clock.RunSim(now)
	if err := db.QueryRow(`SELECT count(*) FROM agent_runs WHERE json_extract(meta,'$.job')='bartervg_bundles'`).Scan(&runs); err != nil || runs != 1 || calls != 0 {
		t.Fatalf("disabled startup tick: runs=%d calls=%d err=%v", runs, calls, err)
	}
	putEpicSchedule(t, mux, `{"bundle_counts":true}`)
	clock.RunSim(now)
	if calls != 1 {
		t.Fatalf("enabling did not kick the task: calls=%d", calls)
	}
	now = epicScheduleTime(t, "2026-11-10 05:40")
	clock.RunSim(now.Add(-time.Second))
	if calls != 1 {
		t.Fatalf("ran before daily time: calls=%d", calls)
	}
	clock.RunSim(now)
	if calls != 2 {
		t.Fatalf("missed daily time: calls=%d", calls)
	}
}

func TestIntervalStartsFromDelayedFirstRun(t *testing.T) {
	now := epicScheduleTime(t, "2026-11-09 14:00")
	first := now.Add(5 * time.Minute)
	var calls int
	automation := &Automation{Job: "auto_backup", Interval: 24 * time.Hour, FirstAfter: 5 * time.Minute,
		Now: func() time.Time { return now }, Run: func(context.Context) (map[string]any, error) { calls++; return nil, nil }}
	clock := sched.NewScheduler()
	defer clock.Stop()
	if err := automation.Register(t.Context(), clock); err != nil {
		t.Fatal(err)
	}
	clock.StartSim(now)
	clock.RunSim(first.Add(-time.Second))
	if calls != 0 {
		t.Fatal("ran before first tick")
	}
	clock.RunSim(first)
	clock.RunSim(first.Add(24*time.Hour - time.Second))
	if calls != 1 {
		t.Fatalf("ran before a full day passed: %d", calls)
	}
	clock.RunSim(first.Add(24 * time.Hour))
	if calls != 2 {
		t.Fatalf("missed daily tick: %d", calls)
	}
}

func epicScheduleTime(t *testing.T, at string) time.Time {
	t.Helper()
	value, err := time.ParseInLocation("2006-01-02 15:04", at, sched.DefaultLoc)
	if err != nil {
		t.Fatal(err)
	}
	return value
}

func putEpicSchedule(t *testing.T, mux *http.ServeMux, body string) {
	t.Helper()
	response := httptest.NewRecorder()
	mux.ServeHTTP(response, httptest.NewRequest("PUT", "/api/v1/settings/fetch", strings.NewReader(body)))
	if response.Code != http.StatusOK {
		t.Fatalf("settings PUT %d: %s", response.Code, response.Body.String())
	}
}

func TestEpicAutomationScheduleDefaults(t *testing.T) {
	for name, raw := range map[string]string{
		"absent":       "",
		"invalid time": `{"time":"25:13","days":30}`,
		"invalid days": `{"time":"23:05","days":"invalid"}`,
		"invalid json": `{`,
	} {
		t.Run(name, func(t *testing.T) {
			db, _, automation := epicScheduleFixture(t)
			if raw != "" {
				if _, err := db.Exec("INSERT INTO app_settings(key,value_json) VALUES('fetch.epic_free_schedule',?)", raw); err != nil {
					t.Fatal(err)
				}
			}
			var fired []string
			clock := sched.NewScheduler(sched.WithOnEvent(func(e sched.Event) {
				if e.Kind == sched.EventFire {
					fired = append(fired, e.Scheduled.In(sched.DefaultLoc).Format("01-02 15:04"))
				}
			}))
			automation.Run = func(context.Context) (map[string]any, error) { return nil, nil }
			if err := automation.Register(t.Context(), clock); err != nil {
				t.Fatal(err)
			}
			clock.StartSim(epicScheduleTime(t, "2026-05-30 23:04").UTC())
			clock.RunSim(epicScheduleTime(t, "2026-05-31 23:05"))
			want := []string{"05-30 23:05", "05-31 23:05"}
			if !reflect.DeepEqual(fired, want) {
				t.Fatalf("fired %v, want %v", fired, want)
			}
		})
	}
}

func TestEpicAutomationSettingsKickAndCalendarCadence(t *testing.T) {
	db, gate, automation := epicScheduleFixture(t)
	var fired []string
	clock := sched.NewScheduler(sched.WithOnEvent(func(e sched.Event) {
		if e.Kind == sched.EventFire {
			fired = append(fired, e.Scheduled.In(sched.DefaultLoc).Format("01-02 15:04"))
		}
	}))
	refreshes := 0
	automation.Run = func(context.Context) (map[string]any, error) {
		refreshes++
		return map[string]any{"refreshed": true}, nil
	}
	now := epicScheduleTime(t, "2026-05-30 23:04")
	automation.Now = func() time.Time { return now }
	mux := http.NewServeMux()
	settings.Register(mux, db, gate, settings.Options{Changed: automation.SettingsChanged})
	putEpicSchedule(t, mux, `{"epic_free":false,"epic_schedule":{"time":"23:05","days":1}}`)
	if err := automation.Register(t.Context(), clock); err != nil {
		t.Fatal(err)
	}
	clock.StartSim(now)
	clock.RunSim(epicScheduleTime(t, "2026-05-30 23:05"))
	if refreshes != 0 {
		t.Fatalf("disabled refresh ran %d times", refreshes)
	}
	var skipped string
	if err := db.QueryRow(`SELECT json_extract(payload,'$.summary.skipped') FROM agent_events WHERE event_type='step.completed'`).Scan(&skipped); err != nil || skipped != "Epic 免费游戏自动更新已关闭" {
		t.Fatalf("recorded skip %q, error %v", skipped, err)
	}

	now = epicScheduleTime(t, "2026-05-30 23:06")
	clock.SimJump(now)
	putEpicSchedule(t, mux, `{"epic_free":true,"epic_schedule":{"time":"8:15","days":10}}`)
	clock.RunSim(now)
	if refreshes != 1 {
		t.Fatalf("enabling ran %d immediate refreshes", refreshes)
	}
	putEpicSchedule(t, mux, `{"epic_free":true}`)
	clock.RunSim(epicScheduleTime(t, "2026-06-11 08:15"))
	want := []string{"05-30 23:05", "05-30 23:06", "05-31 08:15", "06-01 08:15", "06-11 08:15"}
	if !reflect.DeepEqual(fired, want) || refreshes != 4 {
		t.Fatalf("fired %v, refreshes %d, want %v and 4", fired, refreshes, want)
	}
	var done int
	if err := db.QueryRow(`SELECT count(*) FROM agent_runs WHERE runner='job' AND status='done' AND json_extract(meta,'$.job')='epic_free'`).Scan(&done); err != nil || done != len(want) {
		t.Fatalf("recorded %d completed runs, error %v", done, err)
	}

	now = epicScheduleTime(t, "2026-06-11 10:00")
	clock.SimJump(now)
	putEpicSchedule(t, mux, `{"epic_schedule":{"time":"10:01","days":1}}`)
	clock.RunSim(epicScheduleTime(t, "2026-06-11 10:01"))
	if refreshes != 5 {
		t.Fatalf("live reanchor ran %d total refreshes", refreshes)
	}
}

func TestEpicAutomationRestoresStoredCadenceAfterFailure(t *testing.T) {
	db, gate, automation := epicScheduleFixture(t)
	if err := settings.SetValues(t.Context(), gate, settings.Options{}, map[string]any{
		settings.EpicFreeScheduleKey: map[string]any{"time": "6:17", "days": "3"},
	}); err != nil {
		t.Fatal(err)
	}
	attempts := 0
	automation.Run = func(context.Context) (map[string]any, error) {
		attempts++
		if attempts == 1 {
			return nil, errors.New("controlled upstream failure")
		}
		return map[string]any{"refreshed": true}, nil
	}
	var fired []string
	clock := sched.NewScheduler(sched.WithOnEvent(func(e sched.Event) {
		if e.Kind == sched.EventFire {
			fired = append(fired, e.Scheduled.In(sched.DefaultLoc).Format("01-02 15:04"))
		}
	}))
	if err := automation.Register(t.Context(), clock); err != nil {
		t.Fatal(err)
	}
	clock.StartSim(epicScheduleTime(t, "2026-06-03 22:00"))
	clock.RunSim(epicScheduleTime(t, "2026-06-07 06:17"))
	want := []string{"06-04 06:17", "06-07 06:17"}
	if !reflect.DeepEqual(fired, want) || attempts != len(want) {
		t.Fatalf("fired %v with %d attempts, want %v", fired, attempts, want)
	}
	var statuses string
	err := db.QueryRow(`SELECT group_concat(status,',') FROM (SELECT status FROM agent_runs
WHERE json_extract(meta,'$.job')='epic_free' ORDER BY rowid)`).Scan(&statuses)
	if err != nil || statuses != "failed,done" {
		t.Fatalf("recorded statuses %q, error %v", statuses, err)
	}
}

func TestEpicAutomationShutdownSettlesRun(t *testing.T) {
	db, _, automation := epicScheduleFixture(t)
	background, stop := context.WithCancel(t.Context())
	defer stop()
	started := make(chan struct{})
	automation.Run = func(ctx context.Context) (map[string]any, error) {
		close(started)
		<-ctx.Done()
		return nil, ctx.Err()
	}
	clock := sched.NewScheduler()
	defer clock.Stop()
	if err := automation.Register(background, clock); err != nil {
		t.Fatal(err)
	}
	clock.Start()
	automation.SettingsChanged(t.Context(), nil, []string{"epic_free"})
	select {
	case <-started:
	case <-time.After(5 * time.Second):
		t.Fatal("scheduled refresh did not start")
	}
	stop()
	clock.Stop()
	shutdown, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	if err := automation.Close(shutdown); err != nil {
		t.Fatal(err)
	}
	var runStatus, terminalStatus string
	err := db.QueryRow(`SELECT r.status,json_extract(e.payload,'$.status') FROM agent_runs r
JOIN agent_events e ON e.run_id=r.id WHERE json_extract(r.meta,'$.job')='epic_free' AND e.event_type='run.finished'`).Scan(&runStatus, &terminalStatus)
	if err != nil || runStatus != "cancelled" || terminalStatus != "cancelled" {
		t.Fatalf("run status %q, terminal event %q, error %v", runStatus, terminalStatus, err)
	}
}

func TestHumbleUsesOwnDefaultCadenceAndSwitch(t *testing.T) {
	db, gate, epic := epicScheduleFixture(t)
	humble := &Automation{Job: "hb_choice", Label: "Humble Choice 每月合集", Field: "hb_schedule", Key: settings.HumbleChoiceScheduleKey, DB: db}
	var fired []string
	epic.Run = func(context.Context) (map[string]any, error) {
		fired = append(fired, "epic")
		return nil, nil
	}
	humble.Run = func(context.Context) (map[string]any, error) {
		fired = append(fired, "humble")
		return nil, nil
	}
	ctx := t.Context()
	clock := sched.NewScheduler()
	defer clock.Stop()
	for _, automation := range []*Automation{epic, humble} {
		if err := automation.Register(ctx, clock); err != nil {
			t.Fatal(err)
		}
	}
	clock.StartSim(epicScheduleTime(t, "2026-11-08 02:29"))
	clock.RunSim(epicScheduleTime(t, "2026-11-08 02:30"))
	if !reflect.DeepEqual(fired, []string{"humble"}) {
		t.Fatalf("02:30 ran %v", fired)
	}
	mux := http.NewServeMux()
	settings.Register(mux, db, gate)
	putEpicSchedule(t, mux, `{"hb_choice":false}`)
	clock.RunSim(epicScheduleTime(t, "2026-11-09 02:30"))
	if !reflect.DeepEqual(fired, []string{"humble", "epic"}) {
		t.Fatalf("disabled Humble affected Epic or kept running: %v", fired)
	}
}
