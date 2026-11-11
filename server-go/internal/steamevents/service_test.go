package steamevents

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"holdexar/server-go/internal/jobrun"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/store"
)

func eventServiceDB(t *testing.T) (*sql.DB, *store.WriteGate, string) {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "holdexar.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err := store.InitializeFreshDatabase(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	return db, store.NewWriteGate(db, path), dir
}

const demoEnglish = "<h2>Autumn Sale 2026 | November 5 - 12, 2026</h2><h2>Next Fest | November 16 - 23, 2026</h2>"
const demoChinese = "<h2>秋季特卖 | 2026 年 11 月 5 日 - 12 日</h2>"

func TestCalendarManualAndDailyShareDataAndRespectAutomaticSwitch(t *testing.T) {
	db, gate, dir := eventServiceDB(t)
	ctx := context.Background()
	now := time.Date(2026, 11, 8, 4, 59, 59, 0, sched.DefaultLoc)
	var calls atomic.Int32
	s := New(db, gate, Options{DataDir: dir, Now: func() time.Time { return now },
		Recorder: jobrun.New(db, gate, time.Now, func(string, ...any) {}),
		Fetch: func(_ context.Context, language string) (string, error) {
			calls.Add(1)
			if language == "zh" {
				return demoChinese, nil
			}
			return demoEnglish, nil
		}})
	if _, err := db.Exec(`INSERT INTO app_settings(key,value_json) VALUES('fetch.steam_events','false');
		INSERT INTO steam_events(event_key,category,name_en,start_date,end_date,precise_start_ts,store_url)
		VALUES('seasonal_2026_autumn','seasonal_sale','old','2026-11-05','2026-11-12',42,'https://store.steampowered.com/')`); err != nil {
		t.Fatal(err)
	}
	clock := sched.NewScheduler()
	defer clock.Stop()
	if err := s.RegisterSchedules(ctx, clock); err != nil {
		t.Fatal(err)
	}
	clock.StartSim(now)
	clock.RunSim(now.Add(2 * time.Second))
	if calls.Load() != 0 {
		t.Fatal("disabled source used the network")
	}
	mux := http.NewServeMux()
	s.Register(mux)
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/api/v1/steam-events/sync", nil))
	if w.Code != 200 || calls.Load() != 2 {
		t.Fatalf("manual sync: %d %s", w.Code, w.Body)
	}
	w = httptest.NewRecorder()
	mux.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/api/v1/steam-events", nil))
	var list ListResult
	if err := json.Unmarshal(w.Body.Bytes(), &list); err != nil || len(list.Events) != 2 || len(list.Live) != 1 || list.Next == nil || list.Stale {
		t.Fatalf("list: %d %+v %v", w.Code, list, err)
	}
	first := list.Live[0]
	if first.NameZh == nil || *first.NameZh != "秋季特卖" || first.PreciseStart == nil || *first.PreciseStart != 42 || first.StoreURL == nil {
		t.Fatalf("bilingual name or legacy metadata lost: %+v", first)
	}
	if got, err := os.ReadFile(filepath.Join(dir, "steam_events", "upcoming_en.html")); err != nil || string(got) != demoEnglish {
		t.Fatalf("source snapshot: %q %v", got, err)
	}
	if _, err := db.Exec(`UPDATE app_settings SET value_json='true' WHERE key='fetch.steam_events'`); err != nil {
		t.Fatal(err)
	}
	clock.RunSim(now.Add(24*time.Hour + 2*time.Second))
	if calls.Load() != 4 {
		t.Fatalf("daily sync missing: %d calls", calls.Load())
	}
}

func TestCalendarRejectsInvalidEnglishAndRetainsChineseOnPartialFailure(t *testing.T) {
	db, gate, dir := eventServiceDB(t)
	ctx := context.Background()
	now := time.Date(2026, 11, 8, 5, 0, 0, 0, sched.DefaultLoc)
	var brokenEnglish, brokenChinese atomic.Bool
	s := New(db, gate, Options{DataDir: dir, Now: func() time.Time { return now },
		Fetch: func(_ context.Context, language string) (string, error) {
			if language == "zh" {
				if brokenChinese.Load() {
					return "", errors.New("temporary localized failure")
				}
				return demoChinese, nil
			}
			if brokenEnglish.Load() {
				return "<h2>Unavailable</h2>", nil
			}
			return demoEnglish, nil
		}})
	if _, err := s.Sync(ctx); err != nil {
		t.Fatal(err)
	}
	before, err := s.List(ctx)
	if err != nil {
		t.Fatal(err)
	}
	now = now.Add(time.Hour)
	brokenEnglish.Store(true)
	if _, err := s.Sync(ctx); err == nil {
		t.Fatal("invalid authoritative document accepted")
	}
	after, err := s.List(ctx)
	if err != nil || len(after.Live) != 1 || *after.FetchedAt != *before.FetchedAt {
		t.Fatalf("old calendar lost: %+v %v", after, err)
	}
	brokenEnglish.Store(false)
	brokenChinese.Store(true)
	if _, err := s.Sync(ctx); err != nil {
		t.Fatal(err)
	}
	after, err = s.List(ctx)
	if err != nil || len(after.Live) != 1 || after.Live[0].NameZh == nil || *after.Live[0].NameZh != "秋季特卖" || *after.FetchedAt == *before.FetchedAt {
		t.Fatalf("localization or freshness lost: %+v %v", after, err)
	}
}
