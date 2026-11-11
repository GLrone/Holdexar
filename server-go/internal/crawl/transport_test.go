package crawl

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"holdexar/server-go/internal/proxypool"
	"holdexar/server-go/internal/storebrowse"
)

func TestProxyLaneCarriesMetadataPricesAndParentLookups(t *testing.T) {
	db, gate := runtimeFixture(t)
	_, err := db.Exec(`INSERT INTO app_settings(key,value_json) VALUES('proxy.strategy','"proxy_first"')`)
	if err != nil {
		t.Fatal(err)
	}
	var direct atomic.Int32
	origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		direct.Add(1)
		http.Error(w, "unexpected direct request", http.StatusForbidden)
	}))
	t.Cleanup(origin.Close)
	intercepted := make(chan string, 8)
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		var input struct {
			IDs     []storebrowse.ID `json:"ids"`
			Context map[string]any   `json:"context"`
			Data    map[string]any   `json:"data_request"`
		}
		if !req.URL.IsAbs() || origin.URL != req.URL.Scheme+"://"+req.URL.Host || json.Unmarshal([]byte(req.URL.Query().Get("input_json")), &input) != nil || len(input.IDs) != 1 {
			t.Errorf("invalid forwarded request %s", req.URL)
			w.WriteHeader(http.StatusForbidden)
			return
		}
		id := input.IDs[0].AppID
		item := storebrowse.Item{"appid": id, "success": 1, "visible": true, "type": 0, "name": "Proxy fixture"}
		switch {
		case id == 221:
			intercepted <- "parent"
			item["purchase_options"] = []any{map[string]any{"packageid": "39", "purchase_option_name": "Proxy fixture", "final_price_in_cents": "999", "original_price_in_cents": "1999"}}
		case input.Data["include_release"] == true:
			intercepted <- "metadata:" + input.Context["language"].(string)
		default:
			intercepted <- "price"
			item["related_items"] = map[string]any{"parent_appid": 221}
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"response": map[string]any{"store_items": []storebrowse.Item{item}}})
	}))
	t.Cleanup(proxy.Close)
	r := New(db, gate, nil, Options{
		ProxyLanes: func(context.Context) ([]proxypool.CrawlLane, error) {
			return []proxypool.CrawlLane{{ProxyURL: proxy.URL, ExitIP: "192.0.2.42", Node: "fixture"}}, nil
		},
		StoreBrowseURL: origin.URL + "/IStoreBrowseService/GetItems/v1/",
	})
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	cycleID, err := r.Run(ctx, "scheduled", []Spec{{Scope: "appids", AppIDs: []int64{220}}})
	if err != nil {
		t.Fatal(err)
	}
	var price int
	var status string
	if err := db.QueryRow("SELECT price FROM game_current_prices WHERE appid=220 AND region_code='CN'").Scan(&price); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT status FROM price_cycles WHERE id=?", cycleID).Scan(&status); err != nil {
		t.Fatal(err)
	}
	close(intercepted)
	got := map[string]int{}
	for req := range intercepted {
		got[req]++
	}
	if direct.Load() != 0 || price != 999 || status != "completed" || len(got) != 4 || got["metadata:schinese"] != 1 || got["metadata:english"] != 1 || got["price"] != 1 || got["parent"] != 1 {
		t.Fatalf("intercepted=%v direct=%d price=%d status=%s", got, direct.Load(), price, status)
	}
	var runStatus, exitIP, endpoint, node string
	var tasks, successes, requests, responses int
	err = db.QueryRow(`SELECT run.status,run.task_count,run.success_count,exit.exit_ip,exit.endpoint,exit.node,exit.requests,exit.success
		FROM crawl_jobs job JOIN proxy_job_runs run ON run.crawl_job_id=job.id
		JOIN proxy_run_exits exit ON exit.run_id=run.id WHERE job.cycle_id=?`, cycleID).
		Scan(&runStatus, &tasks, &successes, &exitIP, &endpoint, &node, &requests, &responses)
	if err != nil || runStatus != "success" || tasks != 1 || successes != 1 || exitIP != "192.0.2.42" || endpoint != "store_browse" || node != "fixture" || requests != 4 || responses != 4 {
		t.Fatalf("run status=%q tasks=%d successes=%d exit=%q endpoint=%q node=%q requests=%d responses=%d err=%v", runStatus, tasks, successes, exitIP, endpoint, node, requests, responses, err)
	}
}

func TestCrawlJobContinuesWhenProxyTelemetryCannotBeSaved(t *testing.T) {
	for _, statement := range []string{"INSERT", "UPDATE"} {
		t.Run(statement, func(t *testing.T) {
			db, gate := runtimeFixture(t)
			if _, err := db.Exec(`CREATE TRIGGER fail_proxy_telemetry BEFORE ` + statement +
				` ON proxy_job_runs BEGIN SELECT RAISE(FAIL, 'fixture telemetry failure'); END`); err != nil {
				t.Fatal(err)
			}
			var failures atomic.Int32
			r := New(db, gate, nil, Options{
				Log: func(format string, args ...any) {
					if strings.Contains(format, "作业记录") {
						failures.Add(1)
					}
				},
				Fetch: func(_ context.Context, ids []int64, _, _ string, _ bool) (map[int64]storebrowse.Item, error) {
					return map[int64]storebrowse.Item{ids[0]: {"appid": ids[0], "success": 1, "visible": true, "type": 0, "name": "Telemetry fixture", "is_free": true}}, nil
				},
			})
			id, err := r.Run(context.Background(), "scheduled", []Spec{{Scope: "appids", AppIDs: []int64{220}}})
			if err != nil {
				t.Fatal(err)
			}
			var status, observation string
			err = db.QueryRow(`SELECT cycle.status,price.attempt_outcome FROM price_cycles cycle,game_current_prices price
				WHERE cycle.id=? AND price.appid=220 AND price.region_code='CN'`, id).Scan(&status, &observation)
			if err != nil || status != "completed" || observation != "success" || failures.Load() != 1 {
				t.Fatalf("status=%s observation=%s telemetry failures=%d err=%v", status, observation, failures.Load(), err)
			}
		})
	}
}

func TestRecoverMarksOrphanProxyRunsInterrupted(t *testing.T) {
	db, gate := runtimeFixture(t)
	if _, err := db.Exec(`INSERT INTO proxy_job_runs(id,kind,status,started_at,error_summary) VALUES
		(1,'crawl','running','2026-11-08 12:00:00.000000','{"by_error":{"connect":1}}'),
		(2,'crawl','running','2026-11-10 12:00:00.000000',NULL)`); err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, time.November, 9, 12, 0, 0, 0, beijing)
	r := New(db, gate, nil, Options{Now: func() time.Time { return now }})
	if err := r.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	var status, fresh string
	var interrupted, connectErrors int
	err := db.QueryRow(`SELECT status,json_extract(error_summary,'$.interrupted'),json_extract(error_summary,'$.by_error.connect'),
		(SELECT status FROM proxy_job_runs WHERE id=2) FROM proxy_job_runs WHERE id=1`).Scan(&status, &interrupted, &connectErrors, &fresh)
	if err != nil || status != "interrupted" || interrupted != 1 || connectErrors != 1 || fresh != "running" {
		t.Fatalf("status=%q interrupted=%d connect errors=%d later run=%q err=%v", status, interrupted, connectErrors, fresh, err)
	}
}

func TestProxyLaneAdmissionAndFailureNeverUseDirect(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	refused := "http://" + listener.Addr().String()
	listener.Close()
	for _, test := range []struct {
		name  string
		lanes func(context.Context) ([]proxypool.CrawlLane, error)
		jobs  int
	}{
		{name: "missing planner"},
		{name: "empty plan", lanes: func(context.Context) ([]proxypool.CrawlLane, error) { return nil, nil }},
		{name: "planner error", lanes: func(context.Context) ([]proxypool.CrawlLane, error) { return nil, errors.New("controller unavailable") }},
		{name: "invalid lane", lanes: func(context.Context) ([]proxypool.CrawlLane, error) {
			return []proxypool.CrawlLane{{ExitIP: "192.0.2.42", Node: "fixture"}}, nil
		}},
		{name: "listener stopped after admission", jobs: 1, lanes: func(context.Context) ([]proxypool.CrawlLane, error) {
			return []proxypool.CrawlLane{{ProxyURL: refused, ExitIP: "192.0.2.42", Node: "fixture"}}, nil
		}},
	} {
		t.Run(test.name, func(t *testing.T) {
			db, gate := runtimeFixture(t)
			priceFixture(t, db, gate)
			if _, err := db.Exec(`INSERT INTO app_settings(key,value_json) VALUES('proxy.strategy','"proxy_only"')`); err != nil {
				t.Fatal(err)
			}
			var direct atomic.Int32
			origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				direct.Add(1)
				http.Error(w, "unexpected direct request", http.StatusForbidden)
			}))
			t.Cleanup(origin.Close)
			r := New(db, gate, nil, Options{ProxyLanes: test.lanes, StoreBrowseURL: origin.URL, Fetch: func(context.Context, []int64, string, string, bool) (map[int64]storebrowse.Item, error) {
				direct.Add(1)
				return nil, errors.New("unexpected direct fetch")
			}})
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			id, err := r.Run(ctx, "scheduled", []Spec{{Scope: "pool"}})
			if err != nil {
				t.Fatal(err)
			}
			var price, jobs int
			var state string
			if err := db.QueryRow("SELECT price FROM game_current_prices WHERE appid=220 AND region_code='CN'").Scan(&price); err != nil {
				t.Fatal(err)
			}
			if err := db.QueryRow("SELECT count(*) FROM crawl_jobs WHERE cycle_id=?", id).Scan(&jobs); err != nil {
				t.Fatal(err)
			}
			if err := db.QueryRow("SELECT json_extract(specs_json,'$[0].execution.state') FROM price_cycles WHERE id=?", id).Scan(&state); err != nil {
				t.Fatal(err)
			}
			if price != 1999 || jobs != test.jobs || direct.Load() != 0 || state != "deferred" {
				t.Fatalf("price=%d jobs=%d direct=%d state=%s", price, jobs, direct.Load(), state)
			}
		})
	}
}

func TestProxyWorkerCountKeepsAllLanesAvailable(t *testing.T) {
	db, gate := runtimeFixture(t)
	lanes := make([]proxypool.CrawlLane, 9)
	for i := range lanes {
		lanes[i] = proxypool.CrawlLane{
			ProxyURL: fmt.Sprintf("http://127.0.0.1:%d", 20000+i),
			ExitIP:   fmt.Sprintf("192.0.2.%d", i+1),
			Node:     fmt.Sprintf("node-%d", i+1),
		}
	}
	r := New(db, gate, nil, Options{ProxyLanes: func(context.Context) ([]proxypool.CrawlLane, error) {
		return lanes, nil
	}})
	for _, test := range []struct {
		kind, workersJSON string
		want              int
	}{
		{kind: "manual", workersJSON: "null", want: 9},
		{kind: "manual", workersJSON: "2.5", want: 2},
		{kind: "manual", workersJSON: "true", want: 9},
		{kind: "missing", workersJSON: "99", want: 6},
		{kind: "repair", workersJSON: "null", want: 6},
		{kind: "repair", workersJSON: "2", want: 2},
	} {
		if _, err := db.Exec(`INSERT INTO app_settings(key,value_json) VALUES('crawl.workers',?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json`, test.workersJSON); err != nil {
			t.Fatal(err)
		}
		transport, err := r.planProxyTransport(context.Background(), test.kind, &proxyRunTrace{nodes: map[string]string{}})
		if err != nil || transport.workers() != test.want || len(transport.lanes) != len(lanes) {
			t.Fatalf("kind=%s config=%s workers=%d lanes=%d err=%v", test.kind, test.workersJSON, transport.workers(), len(transport.lanes), err)
		}
	}
}

func TestUnavailableProxyRejectsManualHTTPBeforeJobCreation(t *testing.T) {
	db, gate := runtimeFixture(t)
	if _, err := db.Exec(`INSERT INTO app_settings(key,value_json) VALUES('proxy.strategy','"proxy_first"')`); err != nil {
		t.Fatal(err)
	}
	r := New(db, gate, nil, Options{})
	mux := http.NewServeMux()
	r.Register(mux, context.Background())
	req := httptest.NewRequest(http.MethodPost, "/api/v1/crawl/run", strings.NewReader(`{"scope":"appids","appids":[220]}`))
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, req)
	var jobs int
	if err := db.QueryRow("SELECT count(*) FROM crawl_jobs").Scan(&jobs); err != nil {
		t.Fatal(err)
	}
	if rec.Code != http.StatusConflict || jobs != 0 || !strings.Contains(rec.Body.String(), errProxyNotReady.Error()) {
		t.Fatalf("status=%d jobs=%d body=%s", rec.Code, jobs, rec.Body.String())
	}
}

func TestNetworkTakeoverDefersRemainingCycleStagesOnly(t *testing.T) {
	db, gate := runtimeFixture(t)
	checks := 0
	idle := make(chan bool, 2)
	r := New(db, gate, nil, Options{
		AfterIdle: func(stopped bool) { idle <- stopped },
		NetworkBlocked: func(context.Context) bool {
			checks++
			return checks >= 2
		},
		Fetch: func(_ context.Context, ids []int64, _, _ string, _ bool) (map[int64]storebrowse.Item, error) {
			out := map[int64]storebrowse.Item{}
			for _, id := range ids {
				out[id] = storebrowse.Item{"appid": id, "success": 1, "visible": true, "type": 0, "name": "Network fixture", "is_free": true}
			}
			return out, nil
		},
	})
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	id, err := r.Run(ctx, "scheduled", []Spec{
		{Scope: "appids", AppIDs: []int64{220}},
		{Scope: "appids", AppIDs: []int64{221}},
		{Scope: "appids", AppIDs: []int64{222}},
	})
	if err != nil {
		t.Fatal(err)
	}
	var status, encoded string
	if err := db.QueryRow("SELECT status,specs_json FROM price_cycles WHERE id=?", id).Scan(&status, &encoded); err != nil {
		t.Fatal(err)
	}
	var specs []Spec
	if err := json.Unmarshal([]byte(encoded), &specs); err != nil {
		t.Fatal(err)
	}
	if status != "partial" || checks != 2 || specs[0].Execution["state"] != "done" {
		t.Fatalf("status=%s checks=%d specs=%s", status, checks, encoded)
	}
	for _, spec := range specs[1:] {
		reason, _ := spec.Execution["reason"].(string)
		if spec.Execution["state"] != "deferred" || spec.Execution["jobId"] != nil || !strings.Contains(reason, "本机网络被其他软件接管") {
			t.Fatalf("remaining stages=%s", encoded)
		}
	}
	if _, err := r.StartJob(ctx, "appids", []int64{221}, nil, "manual", nil); err != nil {
		t.Fatal(err)
	}
	<-idle // Cycle already completed.
	select {
	case <-idle:
	case <-ctx.Done():
		t.Fatal("standalone job did not settle")
	}
	if checks != 2 {
		t.Fatalf("standalone manual job checked the cycle gate: %d", checks)
	}
}
