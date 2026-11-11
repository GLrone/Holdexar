package crawl

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sync"
	"testing"
	"time"

	"holdexar/server-go/internal/jobrun"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/steamhttp"
	"holdexar/server-go/internal/storebrowse"
)

func seedRecheckGames(t *testing.T, db *sql.DB, n int) {
	t.Helper()
	_, e := db.Exec(`WITH RECURSIVE ids(id) AS(SELECT 1 UNION ALL SELECT id+1 FROM ids WHERE id<?) INSERT INTO games(appid,name,type,family_sharing,trading_cards,is_epic,is_hb,hl_flag,pp_flag,diff_fen,is_adult,is_visual_novel,removed_strikes,positive_reviews,review_count,view_count) SELECT id,'fixture','COMING_SOON',0,0,0,0,0,0,0,0,0,0,0,0,0 FROM ids`, n)
	if e != nil {
		t.Fatal(e)
	}
}

func TestRecheckCandidatesRespectCooldownOrderAndLimits(t *testing.T) {
	db, gate := runtimeFixture(t)
	seedRecheckGames(t, db, 35)
	now := time.Date(2026, 11, 6, 12, 0, 0, 0, beijing)
	r := New(db, gate, nil, Options{Now: func() time.Time { return now }})
	if _, e := db.Exec(`UPDATE games SET updated_at=CASE appid WHEN 1 THEN ? WHEN 2 THEN ? ELSE updated_at END`, timestamp(now.AddDate(0, 0, -14)), timestamp(now.AddDate(0, 0, -13))); e != nil {
		t.Fatal(e)
	}
	ids, e := r.recheckCandidates(context.Background(), "comingsoon_retry")
	if e != nil || len(ids) != 20 || ids[0] != 3 || ids[19] != 22 {
		t.Fatalf("%v %v", ids, e)
	}
	if _, e := db.Exec(`UPDATE games SET free_kind='promo',promo_end_at=CASE appid WHEN 1 THEN NULL WHEN 2 THEN ? ELSE ?+appid END`, now.Unix()+3601, now.Unix()-100); e != nil {
		t.Fatal(e)
	}
	ids, e = r.recheckCandidates(context.Background(), "free_promo_retry")
	if e != nil || len(ids) != 30 || ids[0] != 3 || ids[29] != 32 {
		t.Fatalf("%v %v", ids, e)
	}
}

func TestRecheckUpdatesOriginalPriceAndClearsExpiredPromotion(t *testing.T) {
	db, gate := runtimeFixture(t)
	priceFixture(t, db, gate)
	if _, e := db.Exec(`UPDATE games SET free_kind='promo',promo_end_at=1 WHERE appid=220`); e != nil {
		t.Fatal(e)
	}
	r := New(db, gate, nil, Options{Recorder: jobrun.New(db, gate, time.Now, func(string, ...any) {}), Fetch: func(context.Context, []int64, string, string, bool) (map[int64]storebrowse.Item, error) {
		return map[int64]storebrowse.Item{220: {"appid": 220, "success": 1, "visible": true, "name": "Recovered", "type": 0, "purchase_options": []any{map[string]any{"packageid": "39", "final_price_in_cents": "999", "original_price_in_cents": "1999", "discount_pct": "50"}}}}, nil
	}})
	result, e := r.RunRecheck(context.Background(), "free_promo_retry")
	if e != nil || result["candidates"] != 1 {
		t.Fatalf("%v %v", result, e)
	}
	var free sql.NullString
	var price int64
	if e = db.QueryRow(`SELECT free_kind FROM games WHERE appid=220`).Scan(&free); e != nil || free.Valid {
		t.Fatalf("%v %v", free, e)
	}
	if e = db.QueryRow(`SELECT price FROM game_current_prices WHERE appid=220`).Scan(&price); e != nil || price != 999 {
		t.Fatalf("%d %v", price, e)
	}
	var n int
	if e = db.QueryRow(`SELECT count(*) FROM crawl_jobs WHERE kind='free_promo_retry' AND cycle_id IS NULL AND json_extract(stats_json,'$.trigger')='scheduled'`).Scan(&n); e != nil || n != 1 {
		t.Fatalf("%d %v", n, e)
	}
	if _, e = db.Exec(`UPDATE app_settings SET value_json='false' WHERE key='crawl.auto_price';INSERT INTO app_settings(key,value_json) SELECT 'crawl.auto_price','false' WHERE NOT EXISTS(SELECT 1 FROM app_settings WHERE key='crawl.auto_price');UPDATE games SET free_kind='promo',promo_end_at=1`); e != nil {
		t.Fatal(e)
	}
	result, e = r.RunRecheck(context.Background(), "free_promo_retry")
	if e != nil || result["skipped"] == nil {
		t.Fatalf("%v %v", result, e)
	}
}

func TestRecheckSchedulesAndClosedRuntime(t *testing.T) {
	db, gate := runtimeFixture(t)
	r := New(db, gate, nil, Options{Recorder: jobrun.New(db, gate, time.Now, func(string, ...any) {})})
	clock := sched.NewScheduler()
	if e := r.RegisterSchedules(context.Background(), clock); e != nil {
		t.Fatal(e)
	}
	start := time.Now()
	clock.StartSim(start)
	for i, key := range []string{"comingsoon_retry", "free_promo_retry"} {
		if e := clock.SetNext(key, start.Add(time.Duration(i+1)*time.Second)); e != nil {
			t.Fatal(e)
		}
	}
	clock.RunSim(start.Add(3 * time.Second))
	rows, e := db.Query(`SELECT json_extract(meta,'$.job') FROM agent_runs WHERE status='done' ORDER BY json_extract(meta,'$.job')`)
	if e != nil {
		t.Fatal(e)
	}
	got := []string{}
	for rows.Next() {
		var name string
		if e = rows.Scan(&name); e != nil {
			t.Fatal(e)
		}
		got = append(got, name)
	}
	rows.Close()
	if !reflect.DeepEqual(got, []string{"comingsoon_retry", "free_promo_retry"}) {
		t.Fatal(got)
	}
	if e = r.Close(context.Background()); e != nil {
		t.Fatal(e)
	}
	if _, e = r.RunRecheck(context.Background(), "comingsoon_retry"); !errors.Is(e, context.Canceled) {
		t.Fatal(e)
	}
	if _, e = r.RunDiscovery(context.Background(), "comingsoon_retry", []int64{220}); !errors.Is(e, context.Canceled) {
		t.Fatal(e)
	}
}

func TestRecheckRealTimerHTTPAndPersistedBusinessResults(t *testing.T) {
	db, gate := runtimeFixture(t)
	priceFixture(t, db, gate)
	seedRecheckGames(t, db, 1)
	if _, e := db.Exec(`UPDATE games SET type='COMING_SOON',updated_at='2020-01-01 00:00:00.000000' WHERE appid=220;UPDATE games SET type='GAME',free_kind='promo',promo_end_at=1,updated_at='2026-11-06 00:00:00.000000' WHERE appid=1`); e != nil {
		t.Fatal(e)
	}
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		items := []any{}
		for _, id := range []int64{1, 220} {
			items = append(items, storebrowse.Item{"appid": id, "success": 1, "visible": true, "type": 0, "name": "Rechecked", "purchase_options": []any{map[string]any{"packageid": "39", "final_price_in_cents": "999", "original_price_in_cents": "1999", "discount_pct": "50"}}})
		}
		json.NewEncoder(w).Encode(map[string]any{"response": map[string]any{"store_items": items}})
	}))
	defer upstream.Close()
	client := steamhttp.New(steamhttp.Config{MaxRetries: 1})
	r := New(db, gate, nil, Options{Log: t.Logf, Recorder: jobrun.New(db, gate, time.Now, t.Logf), Fetch: func(ctx context.Context, ids []int64, cc, lang string, extras bool) (map[int64]storebrowse.Item, error) {
		batch, e := storebrowse.FetchAt(ctx, client, upstream.URL, ids, cc, lang, extras, "")
		return batch.Items, e
	}})
	ctx, cancel := context.WithCancel(context.Background())
	clock := sched.NewScheduler()
	defer func() {
		cancel()
		clock.Stop()
		closeCtx, stop := context.WithTimeout(context.Background(), 5*time.Second)
		defer stop()
		if e := r.Close(closeCtx); e != nil {
			t.Error(e)
		}
	}()
	if e := r.RegisterSchedules(ctx, clock); e != nil {
		t.Fatal(e)
	}
	clock.Start()
	for _, kind := range []string{"comingsoon_retry", "free_promo_retry"} {
		if e := clock.SetNext(kind, time.Now().Add(20*time.Millisecond)); e != nil {
			t.Fatal(e)
		}
		deadline := time.Now().Add(8 * time.Second)
		for {
			var n int
			if e := db.QueryRow(`SELECT count(*) FROM agent_runs WHERE json_extract(meta,'$.job')=? AND status='done'`, kind).Scan(&n); e != nil {
				t.Fatal(e)
			}
			if n == 1 {
				break
			}
			if time.Now().After(deadline) {
				t.Fatal("automatic recheck did not finish", kind)
			}
			time.Sleep(20 * time.Millisecond)
		}
	}
	var count int
	if e := db.QueryRow(`SELECT count(*) FROM games WHERE appid IN(1,220) AND type='GAME' AND free_kind IS NULL`).Scan(&count); e != nil || count != 2 {
		t.Fatalf("business results %d %v", count, e)
	}
	if e := db.QueryRow(`SELECT count(*) FROM game_current_prices WHERE appid IN(1,220) AND price=999`).Scan(&count); e != nil || count != 2 {
		t.Fatalf("prices %d %v", count, e)
	}
	mux := http.NewServeMux()
	r.Register(mux, ctx)
	api := httptest.NewServer(mux)
	defer api.Close()
	response, e := http.Get(api.URL + "/api/v1/crawl/jobs")
	if e != nil {
		t.Fatal(e)
	}
	defer response.Body.Close()
	var payload []map[string]any
	if e = json.NewDecoder(response.Body).Decode(&payload); e != nil {
		t.Fatal(e)
	}
	if response.StatusCode != 200 || len(payload) != 2 {
		t.Fatalf("%v", payload)
	}
	t.Logf("automatic jobs=2; records=%v; prices=999; previous coming-soon and expired promotion cleared", len(payload))
}

func TestRecheckShutdownDrainsExecutorAndRunRecord(t *testing.T) {
	db, gate := runtimeFixture(t)
	priceFixture(t, db, gate)
	if _, e := db.Exec(`UPDATE games SET free_kind='promo',promo_end_at=1`); e != nil {
		t.Fatal(e)
	}
	entered := make(chan struct{})
	var once sync.Once
	r := New(db, gate, nil, Options{Recorder: jobrun.New(db, gate, time.Now, func(string, ...any) {}), Fetch: func(ctx context.Context, _ []int64, _, _ string, _ bool) (map[int64]storebrowse.Item, error) {
		once.Do(func() { close(entered) })
		<-ctx.Done()
		return nil, ctx.Err()
	}})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { _, e := r.RunRecheck(ctx, "free_promo_retry"); done <- e }()
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("recheck did not start")
	}
	cancel()
	closeCtx, stop := context.WithTimeout(context.Background(), 5*time.Second)
	defer stop()
	if e := r.Close(closeCtx); e != nil {
		t.Fatal(e)
	}
	if e := <-done; !errors.Is(e, context.Canceled) {
		t.Fatal(e)
	}
	var n int
	if e := db.QueryRow(`SELECT count(*) FROM agent_runs WHERE status='cancelled'`).Scan(&n); e != nil || n != 1 {
		t.Fatalf("settlement %d %v", n, e)
	}
	if e := db.QueryRow(`SELECT count(*) FROM crawl_jobs WHERE status='running'`).Scan(&n); e != nil || n != 0 {
		t.Fatalf("running jobs %d %v", n, e)
	}
}
