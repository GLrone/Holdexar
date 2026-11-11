package boards

import (
	"context"
	"database/sql"
	"errors"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"reflect"
	"testing"
	"time"

	"holdexar/server-go/internal/jobrun"
	"holdexar/server-go/internal/monitoring"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/store"
	"holdexar/server-go/internal/wishlist"
)

func discoveryFixture(t *testing.T, ids []int64, run func(context.Context, string, []int64) (int64, error)) (*Discovery, *sql.DB) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, e := store.Open(path)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { db.Close() })
	if e = store.InitializeFreshDatabase(context.Background(), db); e != nil {
		t.Fatal(e)
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("start") == "0" {
			boardItems(w, ids...)
		} else {
			boardItems(w)
		}
	}))
	t.Cleanup(srv.Close)
	gate := store.NewWriteGate(db, path)
	client := New(Options{Endpoint: srv.URL, Sleep: noBoardDelay})
	pool := wishlist.NewPool(db, gate, monitoring.New(db, gate))
	return NewDiscovery(db, client, pool, DiscoveryOptions{Crawl: run, Recorder: jobrun.New(db, gate, time.Now, func(string, ...any) {})}), db
}

func TestDiscoveryBatchPoolPresetAndIndependentPriceSwitch(t *testing.T) {
	var got []int64
	d, db := discoveryFixture(t, []int64{3, 2, 1}, func(_ context.Context, kind string, ids []int64) (int64, error) {
		if kind != "top100_backfill" {
			t.Fatal(kind)
		}
		got = ids
		return 7, nil
	})
	if _, e := db.Exec(`INSERT INTO app_settings(key,value_json) VALUES('crawl.auto_price','false')`); e != nil {
		t.Fatal(e)
	}
	result, e := d.Run(context.Background(), "topsellers")
	if e != nil || !reflect.DeepEqual(got, []int64{3, 2, 1}) || result["jobId"] != int64(7) {
		t.Fatalf("%v %v %v", result, got, e)
	}
	var count int
	if e = db.QueryRow(`SELECT count(*) FROM monitor_sources WHERE source='board' AND active=1`).Scan(&count); e != nil || count != 3 {
		t.Fatalf("%d %v", count, e)
	}
	if e = db.QueryRow(`SELECT count(*) FROM preset_games WHERE source='topsellers'`).Scan(&count); e != nil || count != 3 {
		t.Fatalf("%d %v", count, e)
	}
	if _, e = db.Exec(`INSERT INTO app_settings(key,value_json) VALUES('fetch.boards','false')`); e != nil {
		t.Fatal(e)
	}
	got = nil
	if _, e = d.Run(context.Background(), "topsellers"); e != nil || got != nil {
		t.Fatalf("%v %v", got, e)
	}
}

func TestDiscoveryBackfillClassifiesAllRowsInRankOrder(t *testing.T) {
	d, db := discoveryFixture(t, nil, nil)
	if _, e := db.Exec(`INSERT INTO games(appid,name,family_sharing,trading_cards,is_epic,is_hb,hl_flag,pp_flag,diff_fen,is_adult,is_visual_novel,removed_strikes,positive_reviews,review_count,view_count) SELECT value,'fixture',0,0,0,0,0,0,0,0,0,0,0,0,0 FROM json_each('[1,2,3,4]'); UPDATE games SET updated_at='2026-11-06' WHERE appid IN(1,4); UPDATE games SET removed_at='2026-11-06' WHERE appid=4; INSERT INTO game_current_prices(appid,region_code,price_status,fail_count,discount_percent) VALUES(3,'CN','missing',1,0)`); e != nil {
		t.Fatal(e)
	}
	ids := []int64{1, 3, 2, 4, 5}
	got, e := d.Backfill(context.Background(), "topsellers", ids, 2)
	if e != nil || !reflect.DeepEqual(got, []int64{2, 4}) {
		t.Fatalf("%v %v", got, e)
	}
	got, e = d.Backfill(context.Background(), "comingsoon", ids, 100)
	if e != nil || !reflect.DeepEqual(got, []int64{4, 5}) {
		t.Fatalf("%v %v", got, e)
	}
}

func TestDiscoverySchedulesRunWithExistingRecorder(t *testing.T) {
	called := 0
	d, db := discoveryFixture(t, []int64{620}, func(context.Context, string, []int64) (int64, error) { called++; return 9, nil })
	start := time.Date(2026, 11, 6, 12, 0, 0, 0, time.UTC)
	d.opts.Now = func() time.Time { return start }
	clock := sched.NewScheduler()
	if e := d.RegisterSchedules(context.Background(), clock); e != nil {
		t.Fatal(e)
	}
	clock.StartSim(start)
	clock.RunSim(start.Add(61 * time.Minute))
	if called != 1 {
		t.Fatal(called)
	}
	var n int
	if e := db.QueryRow(`SELECT count(*) FROM agent_runs WHERE json_extract(meta,'$.job')='board_topsellers' AND status='done'`).Scan(&n); e != nil || n != 1 {
		t.Fatalf("%d %v", n, e)
	}
}

func TestDiscoveryCloseWaitsForCancellationAndRecordsIt(t *testing.T) {
	entered := make(chan struct{})
	d, db := discoveryFixture(t, []int64{620}, func(ctx context.Context, _ string, _ []int64) (int64, error) {
		close(entered)
		<-ctx.Done()
		return 0, ctx.Err()
	})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { _, e := d.Run(ctx, "topsellers"); done <- e }()
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("discovery did not start")
	}
	short, stop := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer stop()
	if e := d.Close(short); !errors.Is(e, context.DeadlineExceeded) {
		t.Fatal(e)
	}
	cancel()
	select {
	case e := <-done:
		if !errors.Is(e, context.Canceled) {
			t.Fatal(e)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("discovery did not cancel")
	}
	if e := d.Close(context.Background()); e != nil {
		t.Fatal(e)
	}
	var n int
	if e := db.QueryRow(`SELECT count(*) FROM agent_runs WHERE status='cancelled'`).Scan(&n); e != nil || n != 1 {
		t.Fatalf("%d %v", n, e)
	}
	if _, e := d.Run(context.Background(), "topsellers"); !errors.Is(e, context.Canceled) {
		t.Fatal(e)
	}
}

func TestDiscoveryPoolWriteFailureStillFeedsBackAndPresetsRollback(t *testing.T) {
	called := 0
	d, db := discoveryFixture(t, []int64{620}, func(context.Context, string, []int64) (int64, error) { called++; return 1, nil })
	if _, e := db.Exec(`CREATE TRIGGER reject_board BEFORE INSERT ON monitor_sources WHEN NEW.source='board' BEGIN SELECT RAISE(ABORT,'controlled pool error'); END; INSERT INTO preset_games(appid,source) VALUES(99,'topsellers');CREATE TRIGGER reject_preset BEFORE INSERT ON preset_games WHEN NEW.appid=620 BEGIN SELECT RAISE(ABORT,'controlled preset error'); END`); e != nil {
		t.Fatal(e)
	}
	result, e := d.Run(context.Background(), "topsellers")
	if e != nil || called != 1 || result["pending"] != 1 {
		t.Fatalf("%v %d %v", result, called, e)
	}
	var n int
	if e := db.QueryRow(`SELECT count(*) FROM preset_games WHERE appid=99 AND source='topsellers'`).Scan(&n); e != nil || n != 1 {
		t.Fatalf("old public preset lost %d %v", n, e)
	}
	if e := db.QueryRow(`SELECT count(*) FROM monitor_targets`).Scan(&n); e != nil || n != 0 {
		t.Fatalf("half pool write %d %v", n, e)
	}
}

func TestAllThreeDiscoveryCallbacksKeepTheirOwnKind(t *testing.T) {
	kinds := []string{}
	d, db := discoveryFixture(t, []int64{620}, func(_ context.Context, kind string, _ []int64) (int64, error) {
		kinds = append(kinds, kind)
		return int64(len(kinds)), nil
	})
	start := time.Date(2026, 11, 6, 12, 0, 0, 0, time.UTC)
	d.opts.Now = func() time.Time { return start }
	clock := sched.NewScheduler()
	if e := d.RegisterSchedules(context.Background(), clock); e != nil {
		t.Fatal(e)
	}
	clock.StartSim(start)
	for i, key := range []string{"topsellers", "popularnew", "comingsoon"} {
		if e := clock.SetNext("board_"+key, start.Add(time.Duration(i+1)*time.Second)); e != nil {
			t.Fatal(e)
		}
	}
	clock.RunSim(start.Add(4 * time.Second))
	if !reflect.DeepEqual(kinds, []string{"top100_backfill", "popularnew_backfill", "comingsoon_backfill"}) {
		t.Fatal(kinds)
	}
	var n int
	if e := db.QueryRow(`SELECT count(*) FROM agent_runs WHERE status='done' AND json_extract(meta,'$.job') IN('board_topsellers','board_popularnew','board_comingsoon')`).Scan(&n); e != nil || n != 3 {
		t.Fatalf("%d %v", n, e)
	}
}
