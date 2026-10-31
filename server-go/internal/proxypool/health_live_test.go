package proxypool

// 真内核三层体检验收（核心门禁）：随包 mihomo.exe 真实拉起（24 节点 + 20 探测
// lane + 独立混合口），节点类型 http → 本桩 HTTP 代理 → 本桩目标，链路全程真实。
// ①L0 经 lane 混合口与控制器 /delay 双路传输探测；②L1 经 lane 出口身份（回环
// 拓扑下身份 = 回环地址，语义正确）；③L2 StoreBrowse 形状本地桩（成功晋升 /
// 目标失败不罚 / 隧道失败判死）；④20 lane 并发两波不饥饿。
// 内核不随仓库分发：assets/clash/mihomo.exe 缺失时整组 skip，不伪装通过。

import (
	"bufio"
	"context"
	"database/sql"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"holdexar/server-go/internal/clashcfg"
	"holdexar/server-go/internal/clashkernel"
	"holdexar/server-go/internal/store"
)

const liveWorktreeRoot = `E:\Holdexar-go-migration`

func liveKernelExe(t *testing.T) string {
	t.Helper()
	exe := filepath.Join(liveWorktreeRoot, "assets", "clash", "mihomo.exe")
	if _, err := os.Stat(exe); err != nil {
		t.Skip("随包内核缺失，跳过真内核三层体检验收")
	}
	return exe
}

const liveNodeCount = 24

func liveNodeName(i int) string { return fmt.Sprintf("ProbeNode-%02d", i+1) }

// liveProbeTarget 业务 / 回显 / 204 三合一目标；browseOK 可切失败档。
type liveProbeTarget struct {
	server   *httptest.Server
	port     int
	browseOK atomic.Bool
	hits     atomic.Int64
}

func newLiveProbeTarget(t *testing.T) *liveProbeTarget {
	t.Helper()
	lt := &liveProbeTarget{}
	lt.browseOK.Store(true)
	mux := http.NewServeMux()
	mux.HandleFunc("/generate_204", func(w http.ResponseWriter, r *http.Request) {
		lt.hits.Add(1)
		w.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("/IStoreBrowseService/GetItems/v1/", func(w http.ResponseWriter, r *http.Request) {
		if !lt.browseOK.Load() {
			w.WriteHeader(http.StatusInternalServerError)
			_, _ = w.Write([]byte("target down"))
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"response":{"store_items":[{"appid":220,"success":1,
			"best_purchase_option":{"final_price_in_cents":"999"}}]}}`))
	})
	mux.HandleFunc("/ip", func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("127.0.0.1"))
	})
	// 固定高位段监听：Windows 动态端口范围（本机 1024-15000）内的监听口
	// 会被高频拨号的隐式源端口绑定撞上（WSAEADDRINUSE），测试目标一律避开。
	listener, port := listenHighPort(t)
	mux2 := mux
	go func() { _ = http.Serve(listener, mux2) }()
	lt.server = &httptest.Server{Listener: listener, Config: &http.Server{Handler: mux}}
	lt.server.URL = fmt.Sprintf("http://127.0.0.1:%d", port)
	lt.port = port
	t.Cleanup(lt.server.Close)
	return lt
}

// listenHighPort 在 ≥20000 段找一个空闲端口并监听。
func listenHighPort(t *testing.T) (net.Listener, int) {
	t.Helper()
	for cand := 20400; cand < 20500; cand++ {
		listener, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", cand))
		if err == nil {
			return listener, cand
		}
	}
	t.Fatal("20400-20499 段没有可用端口")
	return nil, 0
}

// minimalSocks5Server 最小 SOCKS5 服务端（无认证，仅 CONNECT，IPv4/域名）。
func minimalSocks5Server(t *testing.T) (port int) {
	t.Helper()
	listener, p := listenHighPort(t)
	port = p
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			go handleSocks5Conn(conn)
		}
	}()
	t.Cleanup(func() { listener.Close() })
	return port
}

func handleSocks5Conn(conn net.Conn) {
	defer conn.Close()
	r := bufio.NewReader(conn)
	header := make([]byte, 2)
	if _, err := io.ReadFull(r, header); err != nil {
		return
	}
	if header[0] != 5 {
		return
	}
	methods := make([]byte, header[1])
	if _, err := io.ReadFull(r, methods); err != nil {
		return
	}
	conn.Write([]byte{5, 0})
	head := make([]byte, 4)
	if _, err := io.ReadFull(r, head); err != nil {
		return
	}
	var host string
	switch head[3] {
	case 1:
		ip := make([]byte, 4)
		if _, err := io.ReadFull(r, ip); err != nil {
			return
		}
		host = net.IP(ip).String()
	case 3:
		l := make([]byte, 1)
		if _, err := io.ReadFull(r, l); err != nil {
			return
		}
		name := make([]byte, l[0])
		if _, err := io.ReadFull(r, name); err != nil {
			return
		}
		host = string(name)
	default:
		conn.Write([]byte{5, 8, 0, 1, 0, 0, 0, 0, 0, 0})
		return
	}
	portBuf := make([]byte, 2)
	if _, err := io.ReadFull(r, portBuf); err != nil {
		return
	}
	port := binary.BigEndian.Uint16(portBuf)
	upstream, err := net.DialTimeout("tcp", fmt.Sprintf("%s:%d", host, port), 8*time.Second)
	if err != nil {
		conn.Write([]byte{5, 5, 0, 1, 0, 0, 0, 0, 0, 0})
		return
	}
	defer upstream.Close()
	conn.Write([]byte{5, 0, 0, 1, 0, 0, 0, 0, 0, 0})
	done := make(chan struct{}, 2)
	go func() { _, _ = io.Copy(upstream, r); done <- struct{}{} }()
	go func() { _, _ = io.Copy(conn, upstream); done <- struct{}{} }()
	<-done
	<-done
}

func TestLiveHealthThreeLayers(t *testing.T) {
	exe := liveKernelExe(t)
	ctx := context.Background()

	target := newLiveProbeTarget(t)
	targetURL := target.server.URL

	// 24 节点配置 + 20 探测 lane + 独立混合口 / 控制器
	mixedPort, err := clashcfg.FreeLocalPort()
	if err != nil {
		t.Fatal(err)
	}
	controllerPort, err := clashcfg.FreeLocalPort()
	if err != nil {
		t.Fatal(err)
	}
	var proxies strings.Builder
	proxies.WriteString("proxies:\n")
	for i := 0; i < liveNodeCount; i++ {
		// 回环沙箱节点用 direct 出站：真实承载 lane listener → 组选择 →
		// 适配器 → 目标的内核链路（http 节点适配器在本机回环有预拨号失败问题）
		fmt.Fprintf(&proxies, "  - name: %s\n    type: direct\n", liveNodeName(i))
	}
	configText := fmt.Sprintf("mode: global\nlog-level: warning\nallow-lan: false\n"+
		"bind-address: 127.0.0.1\nexternal-controller: 127.0.0.1:%d\nsecret: health-live-secret\n"+
		"mixed-port: %d\n%srules:\n  - MATCH,DIRECT\n", controllerPort, mixedPort, proxies.String())
	configText = clashcfg.InjectProbeLanes(configText, clashcfg.ProbeLaneOptions{LaneCount: 3})

	dir := t.TempDir()
	configPath := filepath.Join(dir, "crawl-runtime.yaml")
	if err := os.WriteFile(configPath, []byte(configText), 0o644); err != nil {
		t.Fatal(err)
	}

	var runtime clashkernel.Runtime
	status, err := runtime.Start(exe, configPath, clashkernel.StartOptions{MixedPort: mixedPort})
	if err != nil {
		t.Fatalf("真内核启动失败：%v", err)
	}
	t.Cleanup(func() { runtime.Stop() })
	if !status.EntryReady {
		t.Fatalf("混合入口未就绪：%+v", status)
	}
	if got := runtime.Port; got != mixedPort {
		t.Fatalf("混合口应为 %d，实际 %d", mixedPort, got)
	}
	if runtime.Secret == "" {
		t.Fatalf("冷启动应生成随机控制器密钥")
	}

	// 账本：临时库 + 24 节点行（runtime 名与内核配置一致）
	db, gate, _ := newTestDB(t)
	conn := mustConn(t, db)
	if err := EnsureSchema(ctx, conn); err != nil {
		t.Fatal(err)
	}
	if err := EnsureHealthSchema(ctx, conn); err != nil {
		t.Fatal(err)
	}
	names := make([]string, liveNodeCount)
	for i := range names {
		names[i] = liveNodeName(i)
		seedNode(t, db, names[i], NodeNew, 0, int64(1))
	}
	checker := &HealthChecker{
		DB: db, Gate: gate, DataDir: dir,
		ControllerURL: runtime.ControllerURL, Secret: runtime.Secret,
		ConfigPath: configPath, Now: func() time.Time { return time.Now() },
	}
	lanes := checker.Lanes()
	if len(lanes) != 3 {
		t.Fatalf("应读到 3 条在听探测 lane，实际 %d", len(lanes))
	}

	// ── ① L0：经各 lane 混合口对本地目标做传输探测，全部可达 ──────────────
	for i, lane := range lanes[:3] {
		node := liveNodeName(i)
		if err := selectLiveGroup(t, runtime.ControllerURL, runtime.Secret, lane.Group, node); err != nil {
			t.Fatalf("lane %s 切组失败：%v", lane.Group, err)
		}
		code, rerr := getViaProxy(mixedProbePort(lane.Port), targetURL+"/generate_204", 8*time.Second)
		if rerr != nil || code != http.StatusNoContent {
			t.Fatalf("L0 经 lane %s 传输探测失败：code=%d err=%v", lane.Group, code, rerr)
		}
	}

	// ── L0 整池（控制器 /delay，health.py 语义）：24 节点全通、延迟挂行 ────
	started := time.Now()
	outcomes, err := checker.L0CheckPool(ctx, names, ProbeOptions{URL: targetURL + "/generate_204", TimeoutMS: 3000})
	if err != nil {
		t.Fatal(err)
	}
	if len(outcomes) != liveNodeCount {
		t.Fatalf("L0 应有 %d 条结论，实际 %d", liveNodeCount, len(outcomes))
	}
	// 回环沙箱观测：mihomo 对回环 socks 链路的 /delay 有约 5-10% 的瞬态 503
	// （失败探测 100% 到达目标并拿到 2xx，属内核回环 URLTest 瞬态，非节点不可
	// 达）。验收对失败节点重走真池路径（重探同时自然补写延迟证据），上限 6 轮。
	transient := 0
	l0Opts := ProbeOptions{URL: targetURL + "/generate_204", TimeoutMS: 3000}
	for round := 0; round < 12; round++ {
		var failed []string
		for _, o := range outcomes {
			if !o.OK {
				failed = append(failed, o.RuntimeName)
			}
		}
		if len(failed) == 0 {
			break
		}
		transient += len(failed)
		if round == 11 {
			for _, o := range outcomes {
				if !o.OK {
					t.Logf("持续不可达：%s detail=%q", o.RuntimeName, o.Detail)
				}
			}
			t.Fatalf("L0 重试 12 轮仍有 %d 个节点不可达（如 %s）", len(failed), failed[0])
		}
		time.Sleep(500 * time.Millisecond)
		retryOutcomes, rerr := checker.L0CheckPool(ctx, failed, l0Opts)
		if rerr != nil {
			t.Fatalf("L0 重试轮失败：%v", rerr)
		}
		byName := map[string]ProbeOutcome{}
		for _, o := range retryOutcomes {
			byName[o.RuntimeName] = o
		}
		for i := range outcomes {
			if o, ok := byName[outcomes[i].RuntimeName]; ok {
				outcomes[i] = o
			}
		}
	}
	for _, o := range outcomes {
		if !o.OK || o.DelayMS <= 0 {
			t.Fatalf("L0 应全部可达：%+v", o)
		}
	}
	if transient > 0 {
		t.Logf("L0 瞬态 503 观测：累计 %d 探次失败（重试后全部可达）", transient)
	}
	if got := queryInt(t, db, `SELECT COUNT(*) FROM proxy_nodes WHERE l0_delay_ms IS NOT NULL`); got != liveNodeCount {
		t.Fatalf("延迟应挂到全部节点行，实际 %d", got)
	}
	// L0 成功不落观测行；失败探次各留一行证据（含重试轮的瞬态失败）。
	if n := countRows(t, db, `SELECT COUNT(*) FROM health_observations`); n != transient {
		t.Fatalf("观测行应等于瞬态失败探次 %d，实际 %d", transient, n)
	}

	// ── ② L1：经 lane 出口身份；回环拓扑下身份 = 回环地址 ─────────────────
	exitOutcomes, err := checker.ExitIPCheckPool(ctx, names, ExitProbeOptions{URL: targetURL + "/ip", Timeout: 10 * time.Second})
	if err != nil {
		t.Fatal(err)
	}
	if len(exitOutcomes) != liveNodeCount {
		t.Fatalf("L1 应有 %d 条结论，实际 %d", liveNodeCount, len(exitOutcomes))
	}
	for _, o := range exitOutcomes {
		if !o.OK || o.ExitIP != "127.0.0.1" {
			t.Fatalf("L1 回环拓扑下出口身份应为 127.0.0.1：%+v", o)
		}
	}
	if got := queryInt(t, db, `SELECT COUNT(*) FROM proxy_nodes WHERE exit_ip = '127.0.0.1' AND last_l1_at IS NOT NULL`); got != liveNodeCount {
		t.Fatalf("出口身份应落全部节点行，实际 %d", got)
	}
	if n := countRows(t, db, `SELECT COUNT(*) FROM health_observations WHERE level = 'L1' AND ok = 1`); n != liveNodeCount {
		t.Fatalf("L1 观测应 %d 行，实际 %d", liveNodeCount, n)
	}

	// ── ③ L2 成功：本地 StoreBrowse 形状桩 → 业务准入晋升 ACTIVE ──────────
	bizStart := time.Now()
	bizOutcomes, err := checker.BusinessCheckPool(ctx, names,
		BusinessProbeOptions{URL: targetURL + "/IStoreBrowseService/GetItems/v1/", Timeout: 15 * time.Second})
	bizElapsed := time.Since(bizStart)
	if err != nil {
		t.Fatal(err)
	}
	for _, o := range bizOutcomes {
		if !o.OK || o.FinalPriceCents != 999 || o.State != NodeActive {
			t.Fatalf("L2 业务成功应晋升 ACTIVE：%+v", o)
		}
	}
	if got := queryInt(t, db, `SELECT COUNT(*) FROM proxy_nodes WHERE state = 'ACTIVE' AND last_l2_at IS NOT NULL`); got != liveNodeCount {
		t.Fatalf("L2 成功应晋升全部节点，实际 %d", got)
	}
	if n := countRows(t, db, `SELECT COUNT(*) FROM health_observations WHERE level = 'L2' AND ok = 1`); n != liveNodeCount {
		t.Fatalf("L2 成功观测应 %d 行，实际 %d", liveNodeCount, n)
	}

	// ── ③ L2 目标失败：Steam 侧 500 归因 target → 不罚节点 ────────────────
	target.browseOK.Store(false)
	failOutcomes, err := checker.BusinessCheckPool(ctx, names,
		BusinessProbeOptions{URL: targetURL + "/IStoreBrowseService/GetItems/v1/", Timeout: 15 * time.Second})
	if err != nil {
		t.Fatal(err)
	}
	for _, o := range failOutcomes {
		if o.OK || o.Attribution != "target" {
			t.Fatalf("目标 500 应归因 target：%+v", o)
		}
	}
	if got := queryInt(t, db, `SELECT COUNT(*) FROM proxy_nodes WHERE state = 'ACTIVE' AND consecutive_failures = 0`); got != liveNodeCount {
		t.Fatalf("目标服务故障不得罚节点，未保持 ACTIVE×0 失败的节点数=%d", got)
	}

	// ── ③ L2 隧道失败：目标整体失联 → 空体 502 → 归因节点 → fail-fast ─────
	target.server.Close()
	tunnelOutcomes, err := checker.BusinessCheckPool(ctx, names,
		BusinessProbeOptions{URL: targetURL + "/IStoreBrowseService/GetItems/v1/", Timeout: 15 * time.Second})
	if err != nil {
		t.Fatal(err)
	}
	for _, o := range tunnelOutcomes {
		if o.OK || o.Attribution != "node" || o.State != NodeDead {
			t.Fatalf("隧道失败应归因节点并判死：%+v", o)
		}
	}
	if got := queryInt(t, db, `SELECT COUNT(*) FROM proxy_nodes WHERE state = 'DEAD' AND consecutive_failures = 1`); got != liveNodeCount {
		t.Fatalf("隧道失败应全部判死计 1 次，实际 %d", got)
	}

	// ── ④ 20 lane 并发不饥饿：两波探测在界内完成 ─────────────────────────
	if bizElapsed > 60*time.Second {
		t.Fatalf("L2 两波（20 lane 并发）耗时 %v，疑似饥饿", bizElapsed)
	}
	l0Elapsed := time.Since(started)
	if l0Elapsed > 90*time.Second {
		t.Fatalf("L0 24 节点（20 并发）累计耗时 %v，疑似饥饿", l0Elapsed)
	}
	t.Logf("实测耗时：L2 两波 %v，L0 全程 %v", bizElapsed, l0Elapsed)

	// 冷却复位语义（真库验证）：DEAD 节点收到成功证据 → 复活清冷却
	var ingest IngestionResult
	alive := CheckRow{Name: names[0], Alive: true, ExitIP: "127.0.0.1", MS: 66, Probed: true}
	if err := gate.WithWrite(ctx, store.PriorityBackground, func(c *sql.Conn) error {
		result, err := IngestNodeCheck(ctx, c, 1, []CheckRow{alive},
			map[string]string{names[0]: "fp-" + names[0]}, time.Now(), 0, "")
		ingest = result
		return err
	}); err != nil {
		t.Fatal(err)
	}
	if ingest.Matched != 1 || ingest.Activated != 1 {
		t.Fatalf("成功证据应复活节点：%+v", ingest)
	}
	if state := queryString(t, db, `SELECT state FROM proxy_nodes WHERE runtime_name = ?`, names[0]); state.String != NodeActive {
		t.Fatalf("复活后应为 ACTIVE，实际 %s", state.String)
	}
}

// ── 实测辅助 ────────────────────────────────────────────────────────────────────────────

func selectLiveGroup(t *testing.T, controllerURL, secret, group, node string) error {
	t.Helper()
	client := &http.Client{Timeout: 8 * time.Second, Transport: &http.Transport{Proxy: nil}}
	do := func(method, path, body string) (int, []byte, error) {
		var reader *strings.Reader
		if body == "" {
			reader = strings.NewReader("")
		} else {
			reader = strings.NewReader(body)
		}
		req, err := http.NewRequest(method, strings.TrimRight(controllerURL, "/")+path, reader)
		if err != nil {
			return 0, nil, err
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", "Bearer "+secret)
		resp, err := client.Do(req)
		if err != nil {
			return 0, nil, err
		}
		defer resp.Body.Close()
		raw := make([]byte, 0, 4096)
		buf := make([]byte, 1024)
		for {
			n, rerr := resp.Body.Read(buf)
			raw = append(raw, buf[:n]...)
			if rerr != nil {
				break
			}
		}
		return resp.StatusCode, raw, nil
	}
	code, _, err := do(http.MethodPut, "/proxies/"+url.PathEscape(group), fmt.Sprintf(`{"name":%q}`, node))
	if err != nil {
		return err
	}
	if code >= 400 {
		return fmt.Errorf("切组 HTTP %d", code)
	}
	code, raw, err := do(http.MethodGet, "/proxies/"+url.PathEscape(group), "")
	if err != nil {
		return err
	}
	if code != 200 {
		return fmt.Errorf("回读 HTTP %d", code)
	}
	var payload struct {
		Now string `json:"now"`
	}
	if json.Unmarshal(raw, &payload) != nil {
		return fmt.Errorf("回读不是 JSON")
	}
	if payload.Now != node {
		return fmt.Errorf("now=%q 不是 %q", payload.Now, node)
	}
	return nil
}

func mixedProbePort(port int) int { return port }

func getViaProxy(port int, target string, timeout time.Duration) (int, error) {
	proxyURL, err := url.Parse(fmt.Sprintf("http://127.0.0.1:%d", port))
	if err != nil {
		return 0, err
	}
	client := &http.Client{
		Timeout:   timeout,
		Transport: &http.Transport{Proxy: http.ProxyURL(proxyURL)},
	}
	resp, err := client.Get(target)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	return resp.StatusCode, nil
}

func parsePort(s string) (int, error) {
	var port int
	if _, err := fmt.Sscanf(s, "%d", &port); err != nil {
		return 0, err
	}
	return port, nil
}
