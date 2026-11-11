package crawl

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"holdexar/server-go/internal/crawler/dbwriter"
	"holdexar/server-go/internal/events"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/steamevents"
	"holdexar/server-go/internal/steamhttp"
	"holdexar/server-go/internal/store"
	"holdexar/server-go/internal/storebrowse"
)

func runtimeFixture(t *testing.T) (*sql.DB, *store.WriteGate) {
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
	if _, err := db.Exec("UPDATE crawl_regions SET enabled=(code='cn')"); err != nil {
		t.Fatal(err)
	}
	return db, store.NewWriteGate(db, path)
}

func priceFixture(t *testing.T, db *sql.DB, gate *store.WriteGate) {
	t.Helper()
	name, kind, cc, currency := "验证游戏", "GAME", "CN", "CNY"
	price, sub := int64(1999), int64(39)
	writer := dbwriter.NewWriter(db, gate)
	writer.Now = func() time.Time { return time.Now().In(beijing).Add(-48 * time.Hour) }
	if _, err := writer.UpsertTaskBatch(context.Background(), []dbwriter.BatchEntry{{Game: dbwriter.GameData{AppID: 220, Name: &name, NameEn: &name, Type: &kind}, Prices: []dbwriter.PriceRow{{AppID: 220, RegionCode: &cc, Currency: &currency, Price: &price, OriginalPrice: &price, SubID: &sub}}}}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO monitor_targets(target_type,target_id) VALUES('game',220); INSERT INTO monitor_sources(target_type,target_id,source,priority,active) VALUES('game',220,'favorite',100,1); INSERT INTO app_settings(key,value_json) VALUES('crawl.queue_discounts','false'),('crawl.queue_missing','false')`); err != nil {
		t.Fatal(err)
	}
}

func TestRuntimeAutomaticScheduleHTTPPriceHistoryEventsAndReads(t *testing.T) {
	db, gate := runtimeFixture(t)
	priceFixture(t, db, gate)
	now := time.Now().In(beijing)
	if _, err := db.Exec(`INSERT INTO steam_events(event_key,category,name_en,start_date,end_date)
		VALUES('seasonal_fixture','seasonal_sale','Fixture sale',?,?)`, now.AddDate(0, 0, -1).Format(time.DateOnly), now.AddDate(0, 0, 1).Format(time.DateOnly)); err != nil {
		t.Fatal(err)
	}
	windows := steamevents.NewWindows(db, gate, nil)
	if err := windows.Reload(context.Background()); err != nil {
		t.Fatal(err)
	}
	var requestMu sync.Mutex
	requests := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		requestMu.Lock()
		requests++
		requestMu.Unlock()
		item := storebrowse.Item{"appid": 220, "success": 1, "visible": true, "type": 0, "name": "验证游戏", "purchase_options": []any{map[string]any{"packageid": "39", "purchase_option_name": "验证游戏", "final_price_in_cents": "999", "original_price_in_cents": "1999", "discount_pct": "50"}}}
		_ = json.NewEncoder(w).Encode(map[string]any{"response": map[string]any{"store_items": []any{item}}})
	}))
	defer upstream.Close()
	client := steamhttp.New(steamhttp.Config{MaxRetries: 1})
	logs := []string{}
	r := New(db, gate, events.NewBus(), Options{EventKey: windows.KeyAt, Log: func(format string, args ...any) {
		requestMu.Lock()
		defer requestMu.Unlock()
		logs = append(logs, fmt.Sprintf(format, args...))
	}, Fetch: func(ctx context.Context, ids []int64, cc, lang string, extras bool) (map[int64]storebrowse.Item, error) {
		batch, e := storebrowse.FetchAt(ctx, client, upstream.URL, ids, cc, lang, extras, "")
		return batch.Items, e
	}})
	scheduler := sched.NewScheduler()
	if err := r.RegisterSchedules(context.Background(), scheduler); err != nil {
		t.Fatal(err)
	}
	scheduler.StartSim(time.Now())
	if err := scheduler.SetNext("price_refresh", time.Now().Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	scheduler.RunSim(time.Now().Add(2 * time.Second))
	scheduler.Stop()
	var price, history, changes int
	if err := db.QueryRow("SELECT price FROM game_current_prices WHERE appid=220 AND region_code='CN'").Scan(&price); err != nil {
		t.Fatal(err)
	}
	_ = db.QueryRow("SELECT count(*) FROM game_price_history WHERE appid=220").Scan(&history)
	_ = db.QueryRow("SELECT count(*) FROM price_events WHERE appid=220 AND event_type='PRICE_DROP'").Scan(&changes)
	if price != 999 || history != 2 || changes != 1 {
		t.Fatalf("price=%d history=%d changes=%d", price, history, changes)
	}
	var tagged int
	if err := db.QueryRow("SELECT count(*) FROM game_price_history WHERE appid=220 AND steam_event_key='seasonal_fixture' AND price=999").Scan(&tagged); err != nil || tagged != 1 {
		t.Fatalf("price observation event tag: %d %v", tagged, err)
	}
	cycles, err := r.Cycles(context.Background(), 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(cycles) != 1 || cycles[0]["status"] != "completed" {
		t.Fatalf("cycles=%#v", cycles)
	}
	coverage, err := r.Coverage(context.Background(), cycles[0]["id"].(int64))
	if err != nil {
		t.Fatal(err)
	}
	if coverage["expectedUnits"] != int64(1) || coverage["ok"] != int64(1) {
		t.Fatalf("coverage=%#v", coverage)
	}
	requestMu.Lock()
	defer requestMu.Unlock()
	if requests != 3 {
		t.Fatalf("requests=%d want 2 metadata + 1 regional price", requests)
	}
	for _, expected := range []string{"价格更新周期 1 已开始", "价格任务 1 已开始，归属周期 1", "价格任务 1 已结束：done", "价格更新周期 1 已结束：completed"} {
		if !strings.Contains(strings.Join(logs, "\n"), expected) {
			t.Errorf("missing correlated log %q in %v", expected, logs)
		}
	}
}

func TestRuntimeHTTPAcceptsManualJobAndStopsWithoutInventingCycle(t *testing.T) {
	db, gate := runtimeFixture(t)
	priceFixture(t, db, gate)
	entered := make(chan struct{})
	var once sync.Once
	r := New(db, gate, events.NewBus(), Options{Fetch: func(ctx context.Context, ids []int64, cc, lang string, extras bool) (map[int64]storebrowse.Item, error) {
		once.Do(func() { close(entered) })
		<-ctx.Done()
		return nil, ctx.Err()
	}})
	accepted, err := r.StartJob(context.Background(), "appids", []int64{220}, nil, "manual", nil)
	if err != nil {
		t.Fatal(err)
	}
	<-entered
	if _, err := r.StartJob(context.Background(), "appids", []int64{220}, nil, "manual", nil); err == nil {
		t.Fatal("second job accepted while running")
	}
	if !r.Stop(accepted["id"].(int64)) {
		t.Fatal("stop refused")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := r.Close(ctx); err != nil {
		t.Fatal(err)
	}
	var status string
	var cycles int
	if err := db.QueryRow("SELECT status FROM crawl_jobs WHERE id=?", accepted["id"]).Scan(&status); err != nil {
		t.Fatal(err)
	}
	_ = db.QueryRow("SELECT count(*) FROM price_cycles").Scan(&cycles)
	if status != "stopped" || cycles != 0 {
		t.Fatal(fmt.Sprintf("status=%s cycles=%d", status, cycles))
	}
}

func TestRuntimeFailedRequestsPreserveLastPriceAndDeferredStage(t *testing.T) {
	db, gate := runtimeFixture(t)
	priceFixture(t, db, gate)
	r := New(db, gate, nil, Options{Fetch: func(context.Context, []int64, string, string, bool) (map[int64]storebrowse.Item, error) {
		return nil, errors.New("controlled upstream failure")
	}})
	id, err := r.Run(context.Background(), "scheduled", []Spec{{Scope: "pool"}})
	if err != nil {
		t.Fatal(err)
	}
	var price, failed int
	var outcome, status, specs string
	if err := db.QueryRow("SELECT price,attempt_outcome FROM game_current_prices WHERE appid=220 AND region_code='CN'").Scan(&price, &outcome); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT status,specs_json FROM price_cycles WHERE id=?", id).Scan(&status, &specs); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT json_extract(stats_json,'$.failed') FROM crawl_jobs WHERE cycle_id=?", id).Scan(&failed); err != nil {
		t.Fatal(err)
	}
	var stages []Spec
	if err := json.Unmarshal([]byte(specs), &stages); err != nil {
		t.Fatal(err)
	}
	if price != 1999 || outcome != "failed" || status != "partial" || failed != 1 || stages[0].Execution["state"] != "deferred" || stages[0].Execution["jobId"] == nil {
		t.Fatalf("price=%d outcome=%s status=%s failed=%d stages=%s", price, outcome, status, failed, specs)
	}
}

func TestRuntimeInterruptedCyclePreservesFrozenRecoveryUnlikeUserStop(t *testing.T) {
	for _, userStop := range []bool{false, true} {
		t.Run(fmt.Sprint(userStop), func(t *testing.T) {
			db, gate := runtimeFixture(t)
			priceFixture(t, db, gate)
			entered := make(chan struct{})
			released := make(chan struct{})
			var once sync.Once
			r := New(db, gate, nil, Options{Fetch: func(ctx context.Context, ids []int64, cc, lang string, extras bool) (map[int64]storebrowse.Item, error) {
				once.Do(func() { close(entered) })
				select {
				case <-ctx.Done():
					return nil, ctx.Err()
				case <-released:
					return nil, errors.New("controlled completion after stop")
				}
			}})
			parent, cancel := context.WithCancel(context.Background())
			defer cancel()
			finished := make(chan error, 1)
			go func() { _, err := r.Run(parent, "scheduled", []Spec{{Scope: "pool"}}); finished <- err }()
			<-entered
			if userStop {
				r.Stop(0)
				close(released)
			} else {
				cancel()
			}
			if err := <-finished; err != nil {
				t.Fatal(err)
			}
			if err := r.Recover(context.Background()); err != nil {
				t.Fatal(err)
			}
			var status, job string
			if err := db.QueryRow("SELECT status FROM price_cycles ORDER BY id DESC LIMIT 1").Scan(&status); err != nil {
				t.Fatal(err)
			}
			if err := db.QueryRow("SELECT status FROM crawl_jobs ORDER BY id DESC LIMIT 1").Scan(&job); err != nil {
				t.Fatal(err)
			}
			pending, err := r.planner.Missing(context.Background(), 0)
			if err != nil {
				t.Fatal(err)
			}
			if userStop {
				if status != "cancelled" || job != "stopped" || len(pending) != 0 {
					t.Fatalf("user status=%s job=%s pending=%#v", status, job, pending)
				}
			} else {
				if status != "failed" || job != "failed" || len(pending) != 1 || pending[0].Region != "cn" {
					t.Fatalf("interrupt status=%s job=%s pending=%#v", status, job, pending)
				}
			}
		})
	}
}

func TestRuntimeSingleJobStopDrainsInFlightPricesWithoutEvents(t *testing.T) {
	db, gate := runtimeFixture(t)
	priceFixture(t, db, gate)
	item := storebrowse.Item{"appid": 220, "success": 1, "visible": true, "type": 0, "name": "验证游戏", "purchase_options": []any{map[string]any{"packageid": "39", "purchase_option_name": "验证游戏", "final_price_in_cents": "999", "original_price_in_cents": "1999", "discount_pct": "50"}}}
	release := make(chan struct{})
	var once sync.Once
	releaseOnce := func() { once.Do(func() { close(release) }) }
	defer releaseOnce()
	active := make(chan context.Context, 1)
	r := New(db, gate, nil, Options{Fetch: func(ctx context.Context, ids []int64, cc, lang string, extras bool) (map[int64]storebrowse.Item, error) {
		if !extras && cc == "us" {
			active <- ctx
			select {
			case <-release:
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}
		return map[int64]storebrowse.Item{220: item}, nil
	}})
	accepted, e := r.StartJob(context.Background(), "appids", []int64{220}, []string{"cn", "us"}, "manual", nil)
	if e != nil {
		t.Fatal(e)
	}
	workCtx := <-active
	deadline := time.Now().Add(5 * time.Second)
	for {
		var price int
		_ = db.QueryRow("SELECT price FROM game_current_prices WHERE appid=220 AND region_code='CN'").Scan(&price)
		if price == 999 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("first region never saved")
		}
		time.Sleep(time.Millisecond)
	}
	if !r.StopJob(accepted["id"].(int64), false) {
		t.Fatal("single stop refused")
	}
	if workCtx.Err() != nil {
		t.Fatal("user stop cancelled the in-flight request instead of draining it")
	}
	releaseOnce()
	for {
		r.mu.Lock()
		busy := r.busy
		r.mu.Unlock()
		if !busy {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("stopped job did not drain")
		}
		time.Sleep(time.Millisecond)
	}
	var status string
	var prices, events int
	_ = db.QueryRow("SELECT status FROM crawl_jobs WHERE id=?", accepted["id"]).Scan(&status)
	_ = db.QueryRow("SELECT count(*) FROM game_current_prices WHERE appid=220 AND price=999").Scan(&prices)
	_ = db.QueryRow("SELECT count(*) FROM price_events").Scan(&events)
	if status != "stopped" || prices != 2 || events != 0 {
		t.Fatalf("status=%s prices=%d events=%d", status, prices, events)
	}
}

func TestRuntimeUnsupportedTypeClearsDebtWithoutLockedPrice(t *testing.T) {
	db, gate := runtimeFixture(t)
	priceFixture(t, db, gate)
	if _, err := db.Exec("UPDATE game_current_prices SET price_status='missing'"); err != nil {
		t.Fatal(err)
	}
	r := New(db, gate, nil, Options{Fetch: func(context.Context, []int64, string, string, bool) (map[int64]storebrowse.Item, error) {
		return map[int64]storebrowse.Item{220: {"appid": 220, "success": 1, "visible": true, "type": 1, "name": "演示版"}}, nil
	}})
	if _, err := r.Run(context.Background(), "scheduled", []Spec{{Scope: "pool"}}); err != nil {
		t.Fatal(err)
	}
	var kind string
	var prices, locked int
	if err := db.QueryRow("SELECT type FROM games WHERE appid=220").Scan(&kind); err != nil {
		t.Fatal(err)
	}
	_ = db.QueryRow("SELECT count(*) FROM game_current_prices WHERE appid=220").Scan(&prices)
	_ = db.QueryRow("SELECT count(*) FROM price_events WHERE event_type='REGION_LOCKED'").Scan(&locked)
	if kind != "DEMO" || prices != 0 || locked != 0 {
		t.Fatalf("kind=%s prices=%d locked=%d", kind, prices, locked)
	}
}

func TestRuntimeManualHTTPToCanonicalReadsAndSSE(t *testing.T) {
	db, gate := runtimeFixture(t)
	priceFixture(t, db, gate)
	bus := events.NewBus()
	sub := bus.Subscribe()
	defer bus.Unsubscribe(sub)
	r := New(db, gate, bus, Options{Fetch: func(context.Context, []int64, string, string, bool) (map[int64]storebrowse.Item, error) {
		return map[int64]storebrowse.Item{220: {"appid": 220, "success": 1, "visible": true, "type": 0, "name": "验证游戏", "purchase_options": []any{map[string]any{"packageid": "39", "purchase_option_name": "验证游戏", "final_price_in_cents": "999", "original_price_in_cents": "1999", "discount_pct": "50"}}}}, nil
	}})
	mux := http.NewServeMux()
	r.Register(mux, context.Background())
	server := httptest.NewServer(mux)
	defer server.Close()
	res, err := http.Post(server.URL+"/api/v1/crawl/run", "application/json", strings.NewReader(`{"scope":"appids","appids":[220]}`))
	if err != nil {
		t.Fatal(err)
	}
	var accepted map[string]any
	err = json.NewDecoder(res.Body).Decode(&accepted)
	res.Body.Close()
	if err != nil || res.StatusCode != 200 || accepted["id"] == nil {
		t.Fatalf("accepted=%v status=%d err=%v", accepted, res.StatusCode, err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		r.mu.Lock()
		busy := r.busy
		r.mu.Unlock()
		if !busy {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("manual job did not finish")
		}
		time.Sleep(time.Millisecond)
	}
	for _, path := range []string{"/crawl/jobs", "/crawl/cycles", "/crawl/queue-specs", "/crawl/freshness/latest", "/crawl/price-events?event_type=PRICE_DROP"} {
		res, err := http.Get(server.URL + "/api/v1" + path)
		if err != nil {
			t.Fatal(err)
		}
		var body any
		err = json.NewDecoder(res.Body).Decode(&body)
		res.Body.Close()
		if err != nil || res.StatusCode != 200 {
			t.Fatalf("%s status=%d body=%v err=%v", path, res.StatusCode, body, err)
		}
		if path == "/crawl/jobs" {
			jobs := body.([]any)
			if len(jobs) != 1 || jobs[0].(map[string]any)["status"] != "done" {
				t.Fatalf("jobs=%v", jobs)
			}
		}
		if path == "/crawl/cycles" && len(body.([]any)) != 0 {
			t.Fatalf("manual job invented cycle=%v", body)
		}
		if path == "/crawl/price-events?event_type=PRICE_DROP" && len(body.([]any)) != 1 {
			t.Fatalf("events=%v", body)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	types := []string{}
	for {
		event, e := sub.Get(ctx)
		if e != nil {
			t.Fatalf("SSE tail=%v err=%v", types, e)
		}
		types = append(types, event.Type)
		if event.Type == "crawl.idle" {
			break
		}
	}
	if len(types) < 3 || types[0] != "job.started" || types[len(types)-2] != "job.status" {
		t.Fatalf("SSE order=%v", types)
	}
}

func TestRuntimeEmptyCycleHasFrozenEmptyResult(t *testing.T) {
	db, gate := runtimeFixture(t)
	r := New(db, gate, events.NewBus(), Options{Fetch: func(context.Context, []int64, string, string, bool) (map[int64]storebrowse.Item, error) {
		t.Fatal("empty pool must not fetch")
		return nil, nil
	}})
	id, err := r.Run(context.Background(), "scheduled", []Spec{{Scope: "pool"}})
	if err != nil {
		t.Fatal(err)
	}
	var status string
	var units int
	if err := db.QueryRow("SELECT status,units_expected FROM price_cycles WHERE id=?", id).Scan(&status, &units); err != nil || status != "completed" || units != 0 {
		t.Fatalf("status=%s units=%d err=%v", status, units, err)
	}
}

func TestRuntimeGridFollowsPacificDSTAndStrictNext(t *testing.T) {
	loc, _ := time.LoadLocation("Asia/Shanghai")
	for _, tc := range []struct {
		now   string
		hours int
		want  string
	}{
		{"2026-07-01T01:00:00+08:00", 6, "2026-07-01T07:00:00+08:00"},
		{"2026-12-01T01:00:00+08:00", 6, "2026-12-01T02:00:00+08:00"},
		{"2026-12-01T03:00:00+08:00", 48, "2026-12-03T02:00:00+08:00"},
	} {
		now, _ := time.Parse(time.RFC3339, tc.now)
		if got := nextPriceGrid(now, tc.hours).In(loc).Format(time.RFC3339); got != tc.want {
			t.Errorf("next(%s,%d)=%s want %s", tc.now, tc.hours, got, tc.want)
		}
	}
}

func TestFetchSettingsReanchorsRegisteredPriceTaskOnlyAfterCommit(t *testing.T) {
	db, gate := runtimeFixture(t)
	ctx := context.Background()
	now := time.Date(2026, 11, 6, 10, 15, 0, 0, beijing)
	r := New(db, gate, events.NewBus(), Options{Now: func() time.Time { return now }})
	var changes []sched.Event
	scheduler := sched.NewScheduler(sched.WithOnEvent(func(e sched.Event) {
		if e.Kind == sched.EventSetNext && e.JobID == "price_refresh" {
			changes = append(changes, e)
		}
	}))
	if err := r.RegisterSchedules(ctx, scheduler); err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	settings.Register(mux, db, gate, settings.Options{Changed: r.SettingsChanged})
	put := func(body string) *httptest.ResponseRecorder {
		w := httptest.NewRecorder()
		mux.ServeHTTP(w, httptest.NewRequest("PUT", "/api/v1/settings/fetch", strings.NewReader(body)))
		return w
	}
	if w := put(`{"price_interval_hours":1}`); w.Code != 200 {
		t.Fatalf("status=%d body=%s", w.Code, w.Body.String())
	}
	want := time.Date(2026, 11, 6, 11, 0, 0, 0, beijing)
	if len(changes) != 1 || !changes[0].Scheduled.Equal(want) {
		t.Fatalf("schedule changes=%v want %s", changes, want)
	}
	if _, err := db.Exec(`CREATE TRIGGER reject_interval BEFORE UPDATE ON app_settings WHEN NEW.key='crawl.price_interval_hours' BEGIN SELECT RAISE(ABORT,'controlled failure'); END`); err != nil {
		t.Fatal(err)
	}
	if w := put(`{"price_interval_hours":2}`); w.Code != 500 {
		t.Fatalf("failed write status=%d body=%s", w.Code, w.Body.String())
	}
	var interval int
	if err := db.QueryRow(`SELECT value_json FROM app_settings WHERE key='crawl.price_interval_hours'`).Scan(&interval); err != nil {
		t.Fatal(err)
	}
	if interval != 1 || len(changes) != 1 {
		t.Fatalf("failed write changed runtime: interval=%d events=%v", interval, changes)
	}
}
