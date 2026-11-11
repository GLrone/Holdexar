package main

import (
	"context"
	"database/sql"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
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

func TestLiveSubscriptionSchedulePromotesAndStartsPriceRepair(t *testing.T) {
	bundleDir := filepath.Join("..", "..", "..", "assets", "clash")
	if _, err := os.Stat(filepath.Join(bundleDir, clashkernel.KernelFilename())); err != nil {
		t.Skip("bundled proxy kernel unavailable")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
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
	origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/ip":
			_, _ = io.WriteString(w, "203.0.113.52")
		case "/IStoreBrowseService/GetItems/v1/":
			_, _ = io.WriteString(w, `{"response":{"store_items":[{"appid":220,"success":1,"visible":true,"type":0,"name":"Subscription fixture","purchase_options":[{"packageid":"39","purchase_option_name":"Subscription fixture","final_price_in_cents":"999","original_price_in_cents":"1999"}],"best_purchase_option":{"final_price_in_cents":"999"}}]}}`)
		default:
			http.NotFound(w, r)
		}
	}))
	defer origin.Close()
	node := localProxyNode(t, origin)
	defer node.Close()
	_, port, _ := net.SplitHostPort(node.Listener.Addr().String())
	var fetched, paused atomic.Int32
	var fail atomic.Bool
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/paused" {
			paused.Add(1)
			http.Error(w, "paused", http.StatusInternalServerError)
			return
		}
		fetched.Add(1)
		if fail.Load() {
			http.Error(w, "unavailable", http.StatusServiceUnavailable)
			return
		}
		w.Header().Set("subscription-userinfo", "upload=0; download=10; total=1000")
		_, _ = fmt.Fprintf(w, "proxies:\n  - name: Fixture\n    type: http\n    server: 127.0.0.1\n    port: %s\n", port)
	}))
	defer provider.Close()
	for id, route := range []string{"/active?token=fixture-secret", "/paused"} {
		sealed, err := proxypool.SealURL(dataDir, provider.URL+route)
		if err != nil {
			t.Fatal(err)
		}
		if _, err = db.ExecContext(ctx, `INSERT INTO proxy_subscriptions(id,url,kind,admission_status,deprecated,auto_refresh)
			VALUES (?,?,'clash','CANDIDATE',0,?)`, id+1, sealed, id == 0); err != nil {
			t.Fatal(err)
		}
	}
	if _, err = db.ExecContext(ctx, `
		UPDATE crawl_regions SET enabled=(code='cn');
		INSERT INTO app_settings(key,value_json) VALUES ('crawl.auto_price','true');
		INSERT INTO game_current_prices(appid,region_code,currency,discount_percent,price_status,fail_count,updated_at)
			VALUES (220,'CN','CNY',0,'missing',0,'2020-01-01 00:00:00');
	`); err != nil {
		t.Fatal(err)
	}
	gate := store.NewWriteGate(db, dbPath)
	recorder := jobrun.New(db, gate, time.Now, t.Logf)
	proxy := newProxySchedule(db, gate, dataDir, bundleDir, recorder, t.Logf)
	proxy.blocked = func(context.Context) bool { return false }
	sub := newSubscriptionSchedule(proxy)
	sub.candidate.Business.URL = "http://steam-fixture.test/IStoreBrowseService/GetItems/v1/"
	sub.candidate.ExitIP.URL = "http://steam-fixture.test/ip"
	price := crawl.New(db, gate, nil, crawl.Options{ProxyLanes: proxy.Lanes, StoreBrowseURL: sub.candidate.Business.URL, Log: t.Logf})
	clock := sched.NewScheduler()
	if err := sub.Register(ctx, clock); err != nil {
		t.Fatal(err)
	}
	if err := price.RegisterSchedules(ctx, clock); err != nil {
		t.Fatal(err)
	}
	if err := clock.SetNext("price_repair", time.Now().Add(time.Minute)); err != nil {
		t.Fatal(err)
	}
	go proxy.run(ctx, clock)
	clock.Start()
	defer func() {
		cancel()
		clock.Stop()
		sub.Close()
		closeCtx, stop := context.WithTimeout(context.Background(), 5*time.Second)
		defer stop()
		if err := price.Close(closeCtx); err != nil {
			t.Error(err)
		}
		<-proxy.done
	}()
	awaitSubscriptionFact(t, ctx, db, `SELECT EXISTS(
		SELECT 1 FROM proxy_subscriptions WHERE id=1 AND admission_status='ACTIVE' AND last_fetch_status='OK')
		AND EXISTS(SELECT 1 FROM game_current_prices WHERE appid=220 AND region_code='CN' AND price=999)
		AND EXISTS(SELECT 1 FROM app_settings WHERE key='proxy.strategy' AND json_extract(value_json,'$')='proxy_first')`)
	t.Logf("automatic subscription admitted, kernel PID %d and price 999", proxy.pid.Load())
	if fetched.Load() != 1 || paused.Load() != 0 {
		t.Fatalf("provider requests active=%d paused=%d", fetched.Load(), paused.Load())
	}
	mux := http.NewServeMux()
	proxypool.RegisterProxyList(mux, db, dataDir)
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, httptest.NewRequest("GET", "/api/v1/proxies", nil))
	if w.Code != 200 || !strings.Contains(w.Body.String(), "total=1000") ||
		!strings.Contains(w.Body.String(), "proxy_first") || strings.Contains(w.Body.String(), "fixture-secret") {
		t.Fatalf("unexpected proxy view %d: %s", w.Code, w.Body)
	}

	if err := clock.SetNext(subscriptionRefreshJob, time.Now().Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	awaitSubscriptionFact(t, ctx, db, `SELECT count(*)>=2 FROM agent_runs
		WHERE json_extract(meta,'$.job')='subscription_refresh' AND status='done'`)
	if fetched.Load() != 1 {
		t.Fatalf("fresh successful subscription downloaded again: %d", fetched.Load())
	}
	fail.Store(true)
	if _, err := db.ExecContext(ctx, "UPDATE proxy_subscriptions SET last_fetch_at='2020-01-01 00:00:00' WHERE id=1"); err != nil {
		t.Fatal(err)
	}
	if err := clock.SetNext(subscriptionRefreshJob, time.Now().Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	awaitSubscriptionFact(t, ctx, db, `SELECT EXISTS(
		SELECT 1 FROM proxy_subscriptions WHERE id=1 AND last_fetch_status='FAILED' AND last_success_at IS NOT NULL
		AND admission_status='ACTIVE' AND json_extract(last_stats,'$.traffic') LIKE '%total=1000%')
		AND EXISTS(SELECT 1 FROM game_current_prices WHERE appid=220 AND region_code='CN' AND price=999)`)
}

func awaitSubscriptionFact(t *testing.T, ctx context.Context, db *sql.DB, query string) {
	t.Helper()
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()
	for ctx.Err() == nil {
		var done bool
		if err := db.QueryRowContext(ctx, query).Scan(&done); err != nil {
			t.Fatal(err)
		}
		if done {
			return
		}
		select {
		case <-ticker.C:
		case <-ctx.Done():
		}
	}
	t.Fatalf("subscription condition timed out: %s", query)
}
