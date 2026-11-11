package proxypool

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"holdexar/server-go/internal/clashcfg"
	"holdexar/server-go/internal/clashkernel"
)

func TestCrawlLanesUseCurrentQualifiedDistinctBoundExits(t *testing.T) {
	ctx := context.Background()
	db, _, dir := poolTestDB(t)
	seedPoolSubscription(t, db, 1, AdmissionActive, "")
	seedPoolSubscription(t, db, 2, AdmissionCandidate, "")
	for _, n := range []struct {
		name, state, ip string
		sub, delay      int
	}{
		{"shared-slow", NodeActive, "192.0.2.1", 1, 80},
		{"shared-fast", NodeActive, "192.0.2.1", 1, 20},
		{"second", NodeStale, "192.0.2.2", 1, 1},
		{"unknown", NodeActive, "", 1, 1},
		{"candidate", NodeActive, "192.0.2.3", 2, 1},
		{"dead", NodeDead, "192.0.2.4", 1, 1},
	} {
		seedPoolNode(t, db, n.name, n.state, int64(n.sub), goodConfig(n.name))
		if _, err := db.Exec(`UPDATE proxy_nodes SET exit_ip = ?, l0_delay_ms = ? WHERE runtime_name = ?`, n.ip, n.delay, n.name); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := BuildPool(ctx, mustConn(t, db), dir, time.Now()); err != nil {
		t.Fatal(err)
	}
	if n, err := CrawlLaneCount(ctx, db, dir); n != 2 || err != nil {
		t.Fatalf("lane count = %d, %v", n, err)
	}
	if lanes, err := ReadCrawlLanes(ctx, db, dir); len(lanes) != 0 || err == nil {
		t.Fatalf("published no plan but returned %v, %v", lanes, err)
	}

	var mu sync.Mutex
	selected := map[string]string{"lane-0": "shared-slow", "lane-1": "shared-slow"}
	controller := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer fixture" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		mu.Lock()
		defer mu.Unlock()
		if r.URL.Path == "/proxies" && r.Method == http.MethodGet {
			proxies := map[string]any{"shared-slow": map[string]any{}, "shared-fast": map[string]any{}, "second": map[string]any{}}
			for group, node := range selected {
				proxies[group] = map[string]string{"now": node}
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"proxies": proxies})
			return
		}
		group := strings.TrimPrefix(r.URL.Path, "/proxies/")
		switch r.Method {
		case http.MethodPut:
			var body struct{ Name string }
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			selected[group] = body.Name
			w.WriteHeader(http.StatusNoContent)
		case http.MethodGet:
			_ = json.NewEncoder(w).Encode(map[string]string{"now": selected[group]})
		}
	}))
	t.Cleanup(controller.Close)
	listen := func() net.Listener {
		t.Helper()
		l, err := net.Listen("tcp4", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = l.Close() })
		return l
	}
	first, second := listen(), listen()
	runtimePath := filepath.Join(dir, "proxypool", clashcfg.RuntimeConfigFilename)
	conf := clashcfg.DumpYAML(map[string]any{
		"external-controller": strings.TrimPrefix(controller.URL, "http://"), "secret": "fixture",
		"listeners": []any{
			map[string]any{"name": "lane-1-in", "proxy": "lane-1", "type": "mixed", "listen": "127.0.0.1", "port": second.Addr().(*net.TCPAddr).Port},
			map[string]any{"name": "lane-0-in", "proxy": "lane-0", "type": "mixed", "listen": "127.0.0.1", "port": first.Addr().(*net.TCPAddr).Port},
		},
	})
	if err := os.WriteFile(runtimePath, []byte(conf), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := BindCrawlLanes(ctx, db, dir, uint32(os.Getpid())); err != nil {
		t.Fatal(err)
	}
	lanes, err := ReadCrawlLanes(ctx, db, dir, uint32(os.Getpid()))
	if err != nil || len(lanes) != 2 || lanes[0] != (CrawlLane{"http://" + first.Addr().String(), "192.0.2.1", "shared-fast"}) ||
		lanes[1] != (CrawlLane{"http://" + second.Addr().String(), "192.0.2.2", "second"}) {
		t.Fatalf("qualified bound lanes = %+v, %v", lanes, err)
	}
	plan, err := os.ReadFile(filepath.Join(dir, "proxypool", "crawl-lanes.yaml"))
	if err != nil || !strings.Contains(string(plan), "- shared-slow") || !strings.Contains(string(plan), "exitIp:") {
		t.Fatalf("compatible plan missing alternate: %q, %v", plan, err)
	}
	if lanes, err := ReadCrawlLanes(ctx, db, dir, 0); len(lanes) != 0 || err == nil {
		t.Fatalf("foreign controller returned %v, %v", lanes, err)
	}

	mu.Lock()
	selected["lane-0"] = "shared-slow"
	mu.Unlock()
	if lanes, err := ReadCrawlLanes(ctx, db, dir); len(lanes) != 0 || err == nil {
		t.Fatalf("selector drift returned %v, %v", lanes, err)
	}
	if err := BindCrawlLanes(ctx, db, dir); err != nil {
		t.Fatal(err)
	}
	conn := mustConn(t, db)
	signature, err := PoolSignature(ctx, conn, dir, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`UPDATE proxy_nodes SET exit_ip = '192.0.2.5' WHERE runtime_name = 'second'`); err != nil {
		t.Fatal(err)
	}
	changed, err := PoolSignature(ctx, conn, dir, time.Now())
	if err != nil || !PoolSignatureChanged(signature, changed) {
		t.Fatalf("same-count exit change did not request rebuild: %v", err)
	}
	if lanes, err := ReadCrawlLanes(ctx, db, dir); len(lanes) != 0 || err == nil {
		t.Fatalf("changed exit returned %v, %v", lanes, err)
	}
	if _, err := db.Exec(`UPDATE proxy_nodes SET exit_ip = '192.0.2.1' WHERE runtime_name = 'second'`); err != nil {
		t.Fatal(err)
	}
	if lanes, err := ReadCrawlLanes(ctx, db, dir); len(lanes) != 0 || err == nil {
		t.Fatalf("merged exits returned %v, %v", lanes, err)
	}
	if _, err := db.Exec(`UPDATE proxy_nodes SET exit_ip = '192.0.2.2' WHERE runtime_name = 'second'`); err != nil {
		t.Fatal(err)
	}
	if err := second.Close(); err != nil {
		t.Fatal(err)
	}
	if lanes, err := ReadCrawlLanes(ctx, db, dir); len(lanes) != 0 || err == nil {
		t.Fatalf("unavailable listener returned %v, %v", lanes, err)
	}
	shortCtx, cancel := context.WithTimeout(ctx, 150*time.Millisecond)
	defer cancel()
	if err := BindCrawlLanes(shortCtx, db, dir); err == nil {
		t.Fatal("bound a plan with an unavailable listener")
	}
	if _, err := os.Stat(filepath.Join(dir, "proxypool", "crawl-lanes.yaml")); !os.IsNotExist(err) {
		t.Fatalf("failed bind retained the old plan: %v", err)
	}
	if _, err := db.Exec(`UPDATE proxy_nodes SET exit_ip = NULL`); err != nil {
		t.Fatal(err)
	}
	if n, err := CrawlLaneCount(ctx, db, dir); n != 0 || err != nil {
		t.Fatalf("probe-only lane count = %d, %v", n, err)
	}
	if err := BindCrawlLanes(ctx, db, dir); err != nil {
		t.Fatalf("probe-only runtime binding: %v", err)
	}
}

func TestLiveCrawlLanesRouteThroughTheirBoundNodes(t *testing.T) {
	exe := liveKernelExe(t)
	ctx := context.Background()
	db, _, dir := poolTestDB(t)
	seedPoolSubscription(t, db, 1, AdmissionActive, "")
	var connections [2]atomic.Int32
	for i := range connections {
		listener, port := listenHighPort(t)
		t.Cleanup(func() { _ = listener.Close() })
		go func() {
			for {
				conn, err := listener.Accept()
				if err != nil {
					return
				}
				connections[i].Add(1)
				go handleSocks5Conn(conn)
			}
		}()
		name := fmt.Sprintf("fixture-%d", i)
		config := fmt.Sprintf(`{"name":%q,"type":"socks5","server":"127.0.0.1","port":%d}`, name, port)
		seedPoolNode(t, db, name, NodeActive, int64(1), config)
		if _, err := db.Exec(`UPDATE proxy_nodes SET exit_ip = ? WHERE runtime_name = ?`, fmt.Sprintf("192.0.2.%d", i+1), name); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := BuildPool(ctx, mustConn(t, db), dir, time.Now()); err != nil {
		t.Fatal(err)
	}
	path, _, err := clashcfg.PrepareRuntimeConfig(dir, 2, false)
	if err != nil {
		t.Fatal(err)
	}
	var runtime clashkernel.Runtime
	if _, err := runtime.Start(exe, path, clashkernel.StartOptions{}); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { runtime.Stop() })
	pid := uint32(runtime.ProcessPid())
	if err := BindCrawlLanes(ctx, db, dir, pid); err != nil {
		t.Fatal(err)
	}
	lanes, err := ReadCrawlLanes(ctx, db, dir, pid)
	if len(lanes) != 2 || err != nil {
		t.Fatalf("live crawl lanes = %v, %v", lanes, err)
	}
	target := newLiveProbeTarget(t)
	for i, lane := range lanes {
		port := crawlPlanPort(t, lane.ProxyURL)
		status, body, _, errType := requestViaPort(ctx, port, target.server.URL+"/ip", 3*time.Second)
		if status != http.StatusOK || body != "127.0.0.1" || connections[i].Load() != 1 {
			t.Fatalf("lane %d via %q returned HTTP %d %q, connections=%d, error=%s", i, lane.Node, status, body, connections[i].Load(), errType)
		}
	}
	t.Logf("kernel routed %d lanes through distinct local SOCKS endpoints", len(lanes))
}

func crawlPlanPort(t *testing.T, proxyURL string) int {
	t.Helper()
	_, raw, err := net.SplitHostPort(strings.TrimPrefix(proxyURL, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	port, err := strconv.Atoi(raw)
	if err != nil {
		t.Fatal(err)
	}
	return port
}

func TestCrawlRuntimeRejectsMixedAndControllerPortsAsLanes(t *testing.T) {
	dir := t.TempDir()
	config := filepath.Join(dir, "proxypool", clashcfg.RuntimeConfigFilename)
	if err := os.MkdirAll(filepath.Dir(config), 0o700); err != nil {
		t.Fatal(err)
	}
	for _, port := range []int{21000, 21001} {
		contents := fmt.Sprintf("external-controller: 127.0.0.1:21000\nmixed-port: 21001\nlisteners:\n"+
			"- name: lane-0-in\n  proxy: lane-0\n  type: mixed\n  listen: 127.0.0.1\n  port: %d\n", port)
		if err := os.WriteFile(config, []byte(contents), 0o600); err != nil {
			t.Fatal(err)
		}
		if _, err := readCrawlRuntime(dir, nil); err == nil {
			t.Fatalf("accepted non-crawl port %d as a crawl lane", port)
		}
	}
}
