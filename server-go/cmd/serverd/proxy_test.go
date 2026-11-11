package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/http/httputil"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"holdexar/server-go/internal/clashkernel"
	"holdexar/server-go/internal/crawl"
	"holdexar/server-go/internal/jobrun"
	"holdexar/server-go/internal/proxypool"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/store"
)

func TestLiveProxyCycleStartsAutomaticPriceRepairThroughOwnedKernel(t *testing.T) {
	bundleDir := filepath.Join("..", "..", "..", "assets", "clash")
	if _, err := os.Stat(filepath.Join(bundleDir, clashkernel.KernelFilename())); err != nil {
		t.Skip("bundled proxy kernel unavailable")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 35*time.Second)
	defer cancel()
	dataDir := t.TempDir()
	dbPath := filepath.Join(dataDir, "holdexar.db")
	db, err := store.Open(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := store.InitializeFreshDatabase(ctx, db); err != nil {
		t.Fatal(err)
	}
	var itemRequests atomic.Int32
	origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/generate_204":
			w.WriteHeader(http.StatusNoContent)
		case "/ip":
			_, _ = io.WriteString(w, "203.0.113.52")
		case "/IStoreBrowseService/GetItems/v1/":
			itemRequests.Add(1)
			_, _ = io.WriteString(w, `{"response":{"store_items":[{"appid":220,"success":1,"visible":true,"type":0,"name":"Proxy fixture","purchase_options":[{"packageid":"39","purchase_option_name":"Proxy fixture","final_price_in_cents":"999","original_price_in_cents":"1999"}],"best_purchase_option":{"final_price_in_cents":"999"}}]}}`)
		default:
			http.NotFound(w, r)
		}
	}))
	defer origin.Close()
	node := localProxyNode(t, origin)
	defer node.Close()
	_, proxyPortText, _ := net.SplitHostPort(node.Listener.Addr().String())
	proxyPort, _ := strconv.Atoi(proxyPortText)
	nodeConfig, err := json.Marshal(map[string]any{"name": "Fixture", "type": "http", "server": "127.0.0.1", "port": proxyPort})
	if err != nil {
		t.Fatal(err)
	}
	sealed, err := proxypool.SealURL(dataDir, "https://subscription.example.test/proxy")
	if err != nil {
		t.Fatal(err)
	}
	if _, err = db.ExecContext(ctx, `
		UPDATE crawl_regions SET enabled=(code='cn');
		INSERT INTO app_settings(key,value_json) VALUES ('proxy.strategy','"proxy_first"'), ('crawl.auto_price','true');
		INSERT INTO game_current_prices(appid,region_code,currency,discount_percent,price_status,fail_count,updated_at)
			VALUES (220,'CN','CNY',0,'missing',0,'2020-01-01 00:00:00');
	`); err != nil {
		t.Fatal(err)
	}
	if _, err = db.ExecContext(ctx, `INSERT INTO proxy_subscriptions(id,url,kind,admission_status,deprecated)
		VALUES (1,?,'clash','ACTIVE',0)`, sealed); err != nil {
		t.Fatal(err)
	}
	if _, err = db.ExecContext(ctx, `
		INSERT INTO proxy_nodes(node_id,runtime_name,original_name,proxy_type,server,normalized_config,state,subscription_id,
			exit_ip,last_l1_at,last_l2_at,consecutive_failures)
			VALUES ('fixture','Fixture','Fixture','http','127.0.0.1',?,'ACTIVE',1,'203.0.113.52',datetime('now'),datetime('now'),0);
	`, string(nodeConfig)); err != nil {
		t.Fatal(err)
	}
	gate := store.NewWriteGate(db, dbPath)
	recorder := jobrun.New(db, gate, time.Now, t.Logf)
	p := newProxySchedule(db, gate, dataDir, bundleDir, recorder, t.Logf)
	p.blocked = func(context.Context) bool { return false }
	target := "http://steam-fixture.test"
	p.cycle.ProbeL0URL, p.cycle.ProbeL1URL, p.cycle.ProbeL2URL = target+"/generate_204", target+"/ip", target+"/IStoreBrowseService/GetItems/v1/"
	price := crawl.New(db, gate, nil, crawl.Options{ProxyLanes: p.Lanes, StoreBrowseURL: p.cycle.ProbeL2URL, Log: t.Logf})
	clock := sched.NewScheduler()
	if err := p.Register(clock); err != nil {
		t.Fatal(err)
	}
	if err := price.RegisterSchedules(ctx, clock); err != nil {
		t.Fatal(err)
	}
	waiting, err := price.Readiness(ctx, p.currentLanes)
	if err != nil || waiting.SchedulerReady || waiting.TransportReady || waiting.Lanes != 0 || waiting.Reason == "" {
		t.Fatalf("unexpected readiness before proxy startup: %+v, err=%v", waiting, err)
	}
	go p.run(ctx, clock)
	clock.Start()
	defer func() {
		cancel()
		clock.Stop()
		closeCtx, stop := context.WithTimeout(context.Background(), 5*time.Second)
		defer stop()
		if err := price.Close(closeCtx); err != nil {
			t.Error(err)
		}
		<-p.done
	}()
	timer := time.NewTicker(100 * time.Millisecond)
	defer timer.Stop()
	for ctx.Err() == nil {
		var cents, cycles, jobs, proxyRun int
		var failure string
		err := db.QueryRowContext(ctx, `SELECT
			COALESCE((SELECT price FROM game_current_prices WHERE appid=220 AND region_code='CN'),0),
			(SELECT COUNT(*) FROM agent_runs WHERE json_extract(meta,'$.job')='proxypool_cycle' AND status='done'),
			(SELECT COUNT(*) FROM crawl_jobs WHERE kind='repair' AND status='done'),
			COALESCE((SELECT id FROM proxy_job_runs WHERE status='success' AND crawl_job_id IN
				(SELECT id FROM crawl_jobs WHERE kind='repair') ORDER BY id DESC LIMIT 1),0),
			COALESCE((SELECT json_extract(e.payload,'$.reason') FROM agent_events e JOIN agent_runs r ON r.id=e.run_id
				WHERE e.event_type='task.failed' AND json_extract(r.meta,'$.job')='proxypool_cycle' ORDER BY e.id DESC LIMIT 1),'')
			`).Scan(&cents, &cycles, &jobs, &proxyRun, &failure)
		if err == nil && failure != "" {
			t.Fatalf("proxy cycle failed: %s", failure)
		}
		if err == nil && cents == 999 && cycles == 1 && jobs == 1 && proxyRun > 0 && itemRequests.Load() >= 4 {
			ready, err := price.Readiness(ctx, p.currentLanes)
			if err != nil || !ready.CollectionReady || ready.Lanes != 1 || ready.Strategy != "proxy_first" || ready.Reason != "" {
				t.Fatalf("unexpected readiness after automatic repair: %+v, err=%v", ready, err)
			}
			mux := http.NewServeMux()
			proxypool.RegisterRuns(mux, db)
			api := httptest.NewServer(mux)
			defer api.Close()
			assertProxyRunEndpoints(t, api, proxyRun)
			t.Logf("owned kernel PID %d, price %d, automatic cycle %d and repair %d, upstream requests %d, proxy run %d", p.pid.Load(), cents, cycles, jobs, itemRequests.Load(), proxyRun)
			return
		}
		select {
		case <-ctx.Done():
		case <-timer.C:
		}
	}
	t.Fatalf("automatic proxy repair did not settle; kernel PID=%d upstream hits=%d", p.pid.Load(), itemRequests.Load())
}

func assertProxyRunEndpoints(t *testing.T, api *httptest.Server, run int) {
	t.Helper()
	get := func(path string, expected int, result any) {
		t.Helper()
		resp, err := api.Client().Get(api.URL + "/api/v1/proxypool/" + path)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		if resp.StatusCode != expected {
			body, _ := io.ReadAll(resp.Body)
			t.Fatalf("%s status %d body %s", path, resp.StatusCode, body)
		}
		if result != nil {
			if err := json.NewDecoder(resp.Body).Decode(result); err != nil {
				t.Fatal(err)
			}
		}
	}
	var list struct {
		Total   int
		Items   []struct{ ID int }
		Summary struct{ Success int }
	}
	get("job-runs", 200, &list)
	if list.Total != 1 || len(list.Items) != 1 || list.Items[0].ID != run || list.Summary.Success != 1 {
		t.Fatalf("wrong run list: %+v", list)
	}
	var detail struct {
		CrawlJobID int
		Exits      []struct {
			ExitIP  string
			Node    string
			Success int
		}
	}
	get(fmt.Sprintf("job-runs/%d", run), 200, &detail)
	if detail.CrawlJobID == 0 || len(detail.Exits) != 1 || detail.Exits[0].ExitIP != "203.0.113.52" || detail.Exits[0].Node != "Fixture" || detail.Exits[0].Success < 3 {
		t.Fatalf("wrong exit telemetry: %+v", detail)
	}
	var health struct{ Runs []struct{ SteamOK int } }
	get("health-runs", 200, &health)
	if len(health.Runs) != 1 || health.Runs[0].SteamOK != 1 {
		t.Fatalf("wrong health list: %+v", health)
	}
	get("job-runs/0", 404, nil)
	get("health-runs?limit=51", 422, nil)
}

func localProxyNode(t *testing.T, origin *httptest.Server) *httptest.Server {
	t.Helper()
	upstream, err := url.Parse(origin.URL)
	if err != nil {
		t.Fatal(err)
	}
	forward := httputil.NewSingleHostReverseProxy(upstream)
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Split(r.Host, ":")[0] != "steam-fixture.test" {
			http.Error(w, "unexpected target", http.StatusForbidden)
			return
		}
		if r.Method != http.MethodConnect {
			forward.ServeHTTP(w, r)
			return
		}
		target, err := net.DialTimeout("tcp", upstream.Host, time.Second)
		if err != nil {
			http.Error(w, "fixture target unavailable", http.StatusBadGateway)
			return
		}
		defer target.Close()
		client, buffer, err := w.(http.Hijacker).Hijack()
		if err != nil {
			return
		}
		defer client.Close()
		if _, err := buffer.WriteString("HTTP/1.1 200 Connection established\r\n\r\n"); err != nil {
			return
		}
		if err := buffer.Flush(); err != nil {
			return
		}
		go func() { _, _ = io.Copy(target, buffer); _ = target.Close() }()
		_, _ = io.Copy(client, target)
	}))
}
