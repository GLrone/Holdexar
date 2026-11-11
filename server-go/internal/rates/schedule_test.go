package rates

import (
	"context"
	"testing"
	"time"

	"holdexar/server-go/internal/jobrun"
	"holdexar/server-go/internal/occupancy"
	"holdexar/server-go/internal/sched"
)

func TestRegisteredAutomaticRefreshUsesTwentySecondStartupAndExistingLedger(t *testing.T) {
	ctx := context.Background()
	now := time.Date(2026, 11, 6, 9, 0, 0, 0, time.FixedZone("Beijing", 8*3600))
	calls := 0
	s, db := serviceFixture(t, ServiceOptions{Now: func() time.Time { return now }, Fetch: func(context.Context, string, time.Duration) ([]byte, error) {
		calls++
		return []byte(`{"USD":7,"EUR":8,"CNY":1,"JPY":0.05,"TRY":0.2}`), nil
	}})
	s.opts.Recorder = jobrun.New(db, s.Gate, s.opts.Now, s.opts.Log)
	clock := sched.NewScheduler()
	if e := s.RegisterSchedules(ctx, clock); e != nil {
		t.Fatal(e)
	}
	clock.StartSim(now)
	clock.RunSim(now.Add(19 * time.Second))
	if calls != 0 {
		t.Fatal("ran before startup anchor")
	}
	clock.RunSim(now.Add(21 * time.Second))
	if calls != 1 {
		t.Fatalf("calls=%d", calls)
	}
	var runs int
	_ = db.QueryRow("SELECT count(*) FROM agent_runs WHERE runner='job' AND json_extract(meta,'$.job')='fx_refresh' AND status='done'").Scan(&runs)
	if runs != 1 {
		t.Fatal(runs)
	}
	if _, e := db.Exec(`INSERT INTO app_settings(key,value_json) VALUES('crawl.auto_refresh_rates','false')`); e != nil {
		t.Fatal(e)
	}
	if e := clock.SetNext("fx_refresh", now.Add(22*time.Second)); e != nil {
		t.Fatal(e)
	}
	clock.RunSim(now.Add(23 * time.Second))
	clock.Stop()
	if calls != 1 {
		t.Fatal("disabled job fetched")
	}
	if e := s.Close(ctx); e != nil {
		t.Fatal(e)
	}
}

func TestHistoricalScheduleAtFourHonorsSwitchAndCrawlerOccupancy(t *testing.T) {
	ctx := context.Background()
	now := time.Date(2026, 11, 6, 3, 59, 58, 0, time.FixedZone("Beijing", 8*3600))
	calls := 0
	s, db := serviceFixture(t, ServiceOptions{Now: func() time.Time { return now }, Fetch: func(context.Context, string, time.Duration) ([]byte, error) {
		calls++
		return []byte(`[{"date":"2026-11-05","quote":"CNY","rate":7}]`), nil
	}})
	s.opts.Recorder = jobrun.New(db, s.Gate, s.opts.Now, s.opts.Log)
	if _, e := db.Exec(`INSERT INTO fx_rate_history(currency_code,rate_date,rate_to_cny,source_kind) VALUES('USD','2026-11-04',9,'observed'),('USD','2026-11-05',9,'carried')`); e != nil {
		t.Fatal(e)
	}
	clock := sched.NewScheduler()
	if e := s.RegisterSchedules(ctx, clock); e != nil {
		t.Fatal(e)
	}
	clock.StartSim(now)
	clock.RunSim(now.Add(3 * time.Second))
	if calls != 1 {
		t.Fatalf("04:00 history calls=%d", calls)
	}
	var rate float64
	_ = db.QueryRow("SELECT rate_to_cny FROM fx_rate_history WHERE currency_code='USD' AND rate_date='2026-11-05'").Scan(&rate)
	if rate != 7 {
		t.Fatal(rate)
	}
	if _, e := db.Exec(`INSERT INTO app_settings(key,value_json) VALUES('fetch.fx_history','false')`); e != nil {
		t.Fatal(e)
	}
	if e := clock.SetNext("fx_history_repair", now.Add(4*time.Second)); e != nil {
		t.Fatal(e)
	}
	clock.RunSim(now.Add(5 * time.Second))
	if calls != 1 {
		t.Fatal("disabled historical job fetched")
	}
	if _, e := db.Exec(`UPDATE app_settings SET value_json='true' WHERE key='fetch.fx_history'; UPDATE fx_rate_history SET source_kind='carried',source='carry' WHERE rate_date='2026-11-05'`); e != nil {
		t.Fatal(e)
	}
	if e := occupancy.BeginCrawl("history-schedule-test"); e != nil {
		t.Fatal(e)
	}
	defer occupancy.EndCrawl()
	if e := clock.SetNext("fx_history_repair", now.Add(6*time.Second)); e != nil {
		t.Fatal(e)
	}
	clock.RunSim(now.Add(7 * time.Second))
	clock.Stop()
	if calls != 1 {
		t.Fatal("busy crawler still fetched historical rates")
	}
	if e := s.Close(ctx); e != nil {
		t.Fatal(e)
	}
}
