package clashservice

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"holdexar/server-go/internal/clashcfg"
	"holdexar/server-go/internal/clashkernel"
	"holdexar/server-go/internal/proxypool"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/store"
)

type stubTestController struct {
	mu      sync.Mutex
	proxies map[string]map[string]any
	putLog  []string
	mode    string
	reject  map[string]bool
}

func newStubController(groups []string) *stubTestController {
	proxies := map[string]map[string]any{}
	for _, g := range groups {
		proxies[g] = map[string]any{"name": g, "type": "selector", "now": "节点A"}
	}
	return &stubTestController{proxies: proxies, mode: "rule", reject: map[string]bool{}}
}

func (c *stubTestController) AllProxies() (map[string]any, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := map[string]any{}
	for name, info := range c.proxies {
		entry := map[string]any{}
		for k, v := range info {
			entry[k] = v
		}
		out[name] = entry
	}
	return map[string]any{"proxies": out}, nil
}

func (c *stubTestController) PutProxy(group, name string) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.reject[group] {
		return fmt.Errorf("组选择被拒（HTTP 400）")
	}
	info := c.proxies[group]
	if info == nil {
		return fmt.Errorf("组不存在（HTTP 404）")
	}
	c.putLog = append(c.putLog, group+":"+name)
	info["now"] = name
	return nil
}

func (c *stubTestController) ProxyNow(group string) (string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	info := c.proxies[group]
	if info == nil {
		return "", fmt.Errorf("组不存在（HTTP 404）")
	}
	now, _ := info["now"].(string)
	return now, nil
}

func (c *stubTestController) CurrentMode() (string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.mode, nil
}

func (c *stubTestController) PatchMode(mode string) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.mode = mode
	return nil
}

type probeCount struct {
	mu    sync.Mutex
	ports map[int]int
}

func (p *probeCount) steam(ctx context.Context, port int) (bool, int) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.ports[port]++
	return true, 200
}

func (p *probeCount) exitIP(ctx context.Context, port int) string {
	return fmt.Sprintf("1.1.1.%d", port%250+1)
}

func (p *probeCount) portsCount() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	total := 0
	for _, n := range p.ports {
		total += n
	}
	return total
}

func nodeFingerprintOf(t *testing.T, configText, url, name string) string {
	t.Helper()
	format, err := proxypool.DetectFormat([]byte(configText))
	if err != nil {
		t.Fatal(err)
	}
	nodes, err := proxypool.ParseNodes([]byte(configText), format)
	if err != nil {
		t.Fatal(err)
	}
	for _, node := range nodes {
		if anyStr(node["name"]) == name {
			fp, ferr := proxypool.NodeFingerprint(node, proxypool.SubscriptionTag(url))
			if ferr != nil {
				t.Fatal(ferr)
			}
			return fp
		}
	}
	t.Fatalf("运行配置里没有节点 %s", name)
	return ""
}

func newTestFixture(t *testing.T, nodes ...string) (*Service, *stubRuntime, *stubTestController, *probeCount, string, string, string) {
	t.Helper()
	if len(nodes) == 0 {
		nodes = []string{"节点A", "节点B"}
	}
	svc, kernel, dir := newFixture(t)
	url := "https://sub.example.com/a"
	seedSubscription(t, svc, 1, url, "A", "", false)

	rawConfig := buildConfigWithNodes(nodes)
	rawPath := SubscriptionConfigPath(dir, 1)
	if err := os.MkdirAll(filepath.Dir(rawPath), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(rawPath, []byte(rawConfig), 0o600); err != nil {
		t.Fatal(err)
	}
	runtimePath, err := MaterializeSubscriptionConfig(rawPath)
	if err != nil {
		t.Fatal(err)
	}
	injected := string(mustRead(t, runtimePath))
	injected = clashcfg.InjectProbeLanes(injected, clashcfg.ProbeLaneOptions{})
	if err := os.WriteFile(runtimePath, []byte(injected), 0o600); err != nil {
		t.Fatal(err)
	}
	lanes, _ := clashcfg.ParseProbeLanes(injected)
	if len(lanes) < 2 {
		t.Fatalf("夹具应注入至少 2 条 lane：%d", len(lanes))
	}
	kernel.mu.Lock()
	kernel.st = clashkernel.Status{
		Running: true, Port: 18347, EntryReady: true,
		ConfigPath: runtimePath, ControllerURL: "http://127.0.0.1:19090",
		SubscriptionURL: url,
	}
	kernel.mu.Unlock()

	groups := []string{"GLOBAL"}
	for _, lane := range lanes {
		groups = append(groups, lane.Group)
	}
	ctl := newStubController(groups)
	ctl.proxies["GLOBAL"] = map[string]any{"name": "GLOBAL", "type": "selector", "now": nodes[0]}
	counts := &probeCount{ports: map[int]int{}}
	svc.SteamProbe = counts.steam
	svc.ExitIPProbe = counts.exitIP
	svc.PortListeningFn = func(int) bool { return true }
	svc.ControllerFactory = func(baseURL, secret string) clashTestController {
		if secret != controllerSecretOf(injected) {
			t.Errorf("控制器密钥应取自运行配置 secret 行")
		}
		return ctl
	}
	for _, name := range nodes {
		seedLedgerNode(t, svc, rawConfig, url, name, proxypool.NodeNew)
	}
	return svc, kernel, ctl, counts, runtimePath, rawConfig, url
}

func mustRead(t *testing.T, path string) []byte {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func buildConfigWithNodes(names []string) string {
	var b strings.Builder
	b.WriteString("mixed-port: 7890\nmode: rule\nrules:\n  - MATCH,DIRECT\nproxies:\n")
	for i, name := range names {
		fmt.Fprintf(&b, "  - name: %q\n    type: ss\n    server: 1.2.3.%d\n    port: 443\n    cipher: aes-128-gcm\n    password: pw\n", name, i+1)
	}
	return b.String()
}

type ledgerSeed struct {
	cooldown *time.Time
	lastL2   *time.Time
	failures int64
	exitIP   string
}

func ledgerCooldown(at time.Time) func(*ledgerSeed) {
	return func(s *ledgerSeed) { s.cooldown = &at }
}

func ledgerLastL2(at time.Time) func(*ledgerSeed) {
	return func(s *ledgerSeed) { s.lastL2 = &at }
}

func ledgerFailures(n int64) func(*ledgerSeed) {
	return func(s *ledgerSeed) { s.failures = n }
}

func ledgerExitIP(ip string) func(*ledgerSeed) {
	return func(s *ledgerSeed) { s.exitIP = ip }
}

func seedLedgerNode(t *testing.T, svc *Service, configText, url, name string, state string, opts ...func(*ledgerSeed)) {
	t.Helper()
	seed := ledgerSeed{}
	for _, opt := range opts {
		opt(&seed)
	}
	fp := nodeFingerprintOf(t, configText, url, name)
	ctx := context.Background()
	err := svc.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		if _, err := conn.ExecContext(ctx, `DELETE FROM proxy_nodes WHERE node_id=?`, fp); err != nil {
			return err
		}
		cooldownArg, lastL2Arg, exitArg := any(nil), any(nil), any(nil)
		if seed.cooldown != nil {
			cooldownArg = pyTextTime(seed.cooldown.In(sched.DefaultLoc))
		}
		if seed.lastL2 != nil {
			lastL2Arg = pyTextTime(seed.lastL2.In(sched.DefaultLoc))
		}
		if seed.exitIP != "" {
			exitArg = seed.exitIP
		}
		_, err := conn.ExecContext(ctx, `INSERT INTO proxy_nodes
			(node_id, runtime_name, proxy_type, server, normalized_config, state,
			 subscription_id, original_name, consecutive_failures, cooldown_until, last_l2_at, exit_ip)
			VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`,
			fp, fmt.Sprintf("1|%s", name), "ss", "1.2.3.4", "{}", state, 1, name,
			seed.failures, cooldownArg, lastL2Arg, exitArg)
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
}

type ledgerRow struct {
	State               string
	ConsecutiveFailures int64
	CooldownUntil       sql.NullString
	ExitIP              sql.NullString
	LastL2At            sql.NullString
}

func readLedgerRow(t *testing.T, svc *Service, configText, url, name string) ledgerRow {
	t.Helper()
	fp := nodeFingerprintOf(t, configText, url, name)
	var row ledgerRow
	err := svc.DB.QueryRow(`SELECT state, consecutive_failures, CAST(cooldown_until AS TEXT),
		exit_ip, CAST(last_l2_at AS TEXT) FROM proxy_nodes WHERE node_id=?`, fp).
		Scan(&row.State, &row.ConsecutiveFailures, &row.CooldownUntil, &row.ExitIP, &row.LastL2At)
	if err != nil {
		t.Fatal(err)
	}
	return row
}

func readSubRow(t *testing.T, svc *Service, id int64) (deprecated bool, reason sql.NullString, stats string) {
	t.Helper()
	err := svc.DB.QueryRow(`SELECT COALESCE(deprecated,0), deprecated_reason, CAST(COALESCE(last_stats,'') AS TEXT)
		FROM proxy_subscriptions WHERE id=?`, id).Scan(&deprecated, &reason, &stats)
	if err != nil {
		t.Fatal(err)
	}
	return deprecated, reason, stats
}

func fixedNow() time.Time {
	return time.Date(2026, 11, 14, 12, 0, 0, 0, time.UTC)
}

func failAllProbe(ctx context.Context, port int) (bool, int) { return false, 0 }

func failAllExitIP(ctx context.Context, port int) string { return "" }

func waitSessionPhase(t *testing.T, svc *Service, phase string, timeout time.Duration) *testSessionSnapshot {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		svc.testMu.Lock()
		var clone *testSessionSnapshot
		if svc.testSession != nil {
			clone = svc.testSession.clone()
		}
		svc.testMu.Unlock()
		if clone != nil && clone.Phase == phase {
			return clone
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("会话未在 %v 内进入 %s", timeout, phase)
	return nil
}

func TestTestProgressIdleShape(t *testing.T) {
	svc, _, _, _, _, _, _ := newTestFixture(t)
	snap := svc.TestProgress(context.Background())
	m, ok := snap.(map[string]any)
	if !ok || m["phase"] != "idle" {
		t.Fatalf("无会话应 {\"phase\":\"idle\"}：%v", snap)
	}
}

func TestTestStartSingleFlightReuse(t *testing.T) {
	svc, _, _, _, _, _, _ := newTestFixture(t)
	release := make(chan struct{})
	entered := make(chan struct{}, 1)
	svc.SteamProbe = func(ctx context.Context, port int) (bool, int) {
		select {
		case entered <- struct{}{}:
		default:
		}
		<-release
		return true, 200
	}
	svc.ExitIPProbe = func(ctx context.Context, port int) string {
		<-release
		return "1.1.1.1"
	}

	first := svc.TestStart(context.Background())
	firstSnap, ok := first.(*testSessionSnapshot)
	if !ok || firstSnap.Phase != "queued" {
		t.Fatalf("首启应落 queued 会话并立即返回：%v", first)
	}
	<-entered

	second := svc.TestStart(context.Background())
	secondSnap, ok := second.(*testSessionSnapshot)
	if !ok {
		t.Fatalf("复用应返回会话快照：%v", second)
	}
	if secondSnap.StartedAt != firstSnap.StartedAt {
		t.Fatalf("active 会话应复用（startedAt 不变）：%s != %s", secondSnap.StartedAt, firstSnap.StartedAt)
	}
	close(release)
	snap := waitSessionPhase(t, svc, "done", 3*time.Second)
	if snap.Probed != 2 {
		t.Fatalf("单飞会话应恰好完成一轮探测：probed=%d", snap.Probed)
	}
}

func TestTestStartAfterDoneRunsNewSession(t *testing.T) {
	svc, _, _, _, _, _, _ := newTestFixture(t)
	svc.TestStart(context.Background())
	waitSessionPhase(t, svc, "done", 3*time.Second)

	svc.TestStart(context.Background())
	svc.testMu.Lock()
	second := svc.testSession.clone()
	svc.testMu.Unlock()
	if second.Phase != "queued" && second.Phase != "running" {
		t.Fatalf("done 后再开应落新会话（queued/running），实际 %s", second.Phase)
	}
	waitSessionPhase(t, svc, "done", 3*time.Second)
}

func TestTestSessionQueuedVisibleDuringLockHold(t *testing.T) {
	svc, _, _, _, _, _, _ := newTestFixture(t)
	svc.TestLock.Lock()
	svc.TestStart(context.Background())
	svc.testMu.Lock()
	phase := svc.testSession.Phase
	svc.testMu.Unlock()
	if phase != "queued" {
		t.Fatalf("锁被占时 start 应返回 queued 会话：%s", phase)
	}
	svc.TestLock.Unlock()
	waitSessionPhase(t, svc, "done", 3*time.Second)
}

func TestTestSessionFailedWithoutKernel(t *testing.T) {
	svc, kernel, _, _, _, _, _ := newTestFixture(t)
	kernel.mu.Lock()
	kernel.st = clashkernel.Status{}
	kernel.mu.Unlock()
	svc.TestStart(context.Background())
	snap := waitSessionPhase(t, svc, "failed", 3*time.Second)
	if snap.Error == nil || !strings.Contains(*snap.Error, "Clash 未运行") {
		t.Fatalf("失败收口应带用户文案：%v", snap.Error)
	}
	if snap.FinishedAt == nil {
		t.Fatalf("终态应带 finishedAt")
	}
}

func TestTestSessionProgressSnapshotFields(t *testing.T) {
	svc, _, _, _, _, _, _ := newTestFixture(t)
	svc.TestStart(context.Background())
	snap := waitSessionPhase(t, svc, "done", 3*time.Second)
	if snap.Total == nil || *snap.Total != 2 {
		t.Fatalf("total 应为节点数 2：%v", snap.Total)
	}
	if snap.ToProbe == nil || *snap.ToProbe != 2 {
		t.Fatalf("toProbe 应为 2（手动检测全量）：%v", snap.ToProbe)
	}
	if snap.Probed != 2 || snap.Alive != 2 || snap.AliveUnique == nil || *snap.AliveUnique != 2 {
		t.Fatalf("探测统计不符：probed=%d alive=%d aliveUnique=%v", snap.Probed, snap.Alive, snap.AliveUnique)
	}
	if snap.Selector == nil || *snap.Selector != "GLOBAL" {
		t.Fatalf("selector 应为路由组名：%v", snap.Selector)
	}
	if snap.SubscriptionID == nil || *snap.SubscriptionID != 1 {
		t.Fatalf("subscriptionId 应为 1：%v", snap.SubscriptionID)
	}
	if snap.Deprecated == nil || *snap.Deprecated {
		t.Fatalf("有活节点不应废弃：%v", snap.Deprecated)
	}
	if len(snap.Nodes) != 2 {
		t.Fatalf("nodes 应含两行：%d", len(snap.Nodes))
	}
	for _, row := range snap.Nodes {
		if !row.Probed || !row.Alive || !row.SteamOK || row.SteamStatus == nil || *row.SteamStatus != 200 ||
			row.ExitIP == nil || row.MS == nil {
			t.Fatalf("节点行字段不全：%+v", row)
		}
		if row.PoolState == nil || *row.PoolState != proxypool.NodeActive {
			t.Fatalf("落账后应带 poolState=ACTIVE：%+v", row)
		}
		if row.SourceCode == nil || *row.SourceCode != "sd1" {
			t.Fatalf("落账后应带 sourceCode=sd1：%+v", row)
		}
	}
	if snap.StartedAt == "" || snap.FinishedAt == nil || snap.Error != nil {
		t.Fatalf("起止时刻/错误位不符：started=%q finished=%v error=%v", snap.StartedAt, snap.FinishedAt, snap.Error)
	}
	raw, err := json.Marshal(svc.TestProgress(context.Background()))
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(raw, &decoded); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"phase", "total", "toProbe", "probed", "cooldownSkipped", "alive",
		"aliveUnique", "selector", "subscriptionId", "deprecated", "nodes", "startedAt", "finishedAt", "error"} {
		if _, ok := decoded[key]; !ok {
			t.Fatalf("快照缺 camelCase 字段 %s：%s", key, raw)
		}
	}
}

func TestCooldownSkipCountsAndPayload(t *testing.T) {
	svc, _, _, _, _, configText, url := newTestFixture(t)
	now := fixedNow()
	seedLedgerNode(t, svc, configText, url, "节点A", proxypool.NodeActive,
		ledgerCooldown(now.Add(30*time.Minute)), ledgerLastL2(now))
	seedLedgerNode(t, svc, configText, url, "节点B", proxypool.NodeNew)

	result, err := svc.testClashNodes(context.Background(), nil, false)
	if err != nil {
		t.Fatal(err)
	}
	svc.testMu.Lock()
	snap := svc.testSession.clone()
	svc.testMu.Unlock()
	if snap.CooldownSkipped != 1 {
		t.Fatalf("冷却跳过应计数 1：%d", snap.CooldownSkipped)
	}
	if *snap.ToProbe != 1 || *snap.Total != 2 {
		t.Fatalf("toProbe=1 total=2，实际 toProbe=%d total=%d", *snap.ToProbe, *snap.Total)
	}
	var cooling *testNodeRow
	for i := range result.Nodes {
		if result.Nodes[i].Cooling {
			cooling = &result.Nodes[i]
		}
	}
	if cooling == nil || cooling.Name != "节点A" || !cooling.Alive || cooling.Probed {
		t.Fatalf("冷却行应为 节点A cooling+账本存活：%+v", cooling)
	}
	if len(result.Nodes) != 2 {
		t.Fatalf("结果应含实探+冷却两行：%d", len(result.Nodes))
	}
}

func TestLedgerCooldownLadderAndDeprecation(t *testing.T) {
	svc, _, _, _, _, configText, url := newTestFixture(t)
	svc.SteamProbe = failAllProbe
	svc.ExitIPProbe = failAllExitIP
	now := fixedNow()
	ladder := []int{30, 60, 120, 240, 480, 1440, 1440, 1440}
	for _, expectMinutes := range ladder {
		if _, err := svc.testClashNodes(context.Background(), nil, true); err != nil {
			t.Fatal(err)
		}
		row := readLedgerRow(t, svc, configText, url, "节点A")
		if !row.CooldownUntil.Valid || row.CooldownUntil.String == "" {
			t.Fatalf("失败后应落冷却")
		}
		until, ok := parsePyTime(row.CooldownUntil.String)
		if !ok {
			t.Fatalf("冷却时间不可解析：%q", row.CooldownUntil.String)
		}
		delta := until.Sub(now).Round(time.Minute)
		if delta != time.Duration(expectMinutes)*time.Minute {
			t.Fatalf("连续失败 %d 次期望冷却 %dmin，实际 %v", row.ConsecutiveFailures, expectMinutes, delta)
		}
		deprecated, reason, _ := readSubRow(t, svc, 1)
		if !deprecated || !strings.Contains(reason.String, "没有可用节点") {
			t.Fatalf("全灭应废弃订阅带原因：%v %v", deprecated, reason)
		}
	}
}

func TestLedgerSuccessResetsCooldownAndRecoversSubscription(t *testing.T) {
	svc, _, _, _, _, configText, url := newTestFixture(t)
	svc.SteamProbe = failAllProbe
	svc.ExitIPProbe = failAllExitIP
	for i := 0; i < 2; i++ {
		if _, err := svc.testClashNodes(context.Background(), nil, true); err != nil {
			t.Fatal(err)
		}
	}
	if deprecated, _, _ := readSubRow(t, svc, 1); !deprecated {
		t.Fatalf("全灭后订阅应废弃")
	}
	row := readLedgerRow(t, svc, configText, url, "节点A")
	if row.State != proxypool.NodeDead || row.ConsecutiveFailures != 2 {
		t.Fatalf("两轮失败后应为 DEAD×2：%s %d", row.State, row.ConsecutiveFailures)
	}
	svc.SteamProbe = func(ctx context.Context, port int) (bool, int) { return true, 200 }
	svc.ExitIPProbe = func(ctx context.Context, port int) string { return "2.2.2.2" }
	if _, err := svc.testClashNodes(context.Background(), nil, true); err != nil {
		t.Fatal(err)
	}
	row = readLedgerRow(t, svc, configText, url, "节点A")
	if row.CooldownUntil.Valid && row.CooldownUntil.String != "" {
		t.Fatalf("成功后应清冷却：%v", row.CooldownUntil)
	}
	if row.State != proxypool.NodeActive || row.ConsecutiveFailures != 0 {
		t.Fatalf("成功应复活并清零失败计数：%s %d", row.State, row.ConsecutiveFailures)
	}
	if !row.ExitIP.Valid || row.ExitIP.String != "2.2.2.2" {
		t.Fatalf("成功应落出口 IP：%v", row.ExitIP)
	}
	deprecated, reason, _ := readSubRow(t, svc, 1)
	if deprecated || reason.Valid {
		t.Fatalf("有活节点应自动解除废弃：%v %v", deprecated, reason)
	}
}

func TestLedgerRetireAfterThreeFailuresAndRestore(t *testing.T) {
	svc, _, _, _, _, configText, url := newTestFixture(t)
	svc.SteamProbe = failAllProbe
	svc.ExitIPProbe = failAllExitIP
	for i := 0; i < 3; i++ {
		if _, err := svc.testClashNodes(context.Background(), nil, true); err != nil {
			t.Fatal(err)
		}
	}
	row := readLedgerRow(t, svc, configText, url, "节点A")
	if row.State != proxypool.NodeRetired || row.ConsecutiveFailures != 3 {
		t.Fatalf("连续 3 次失败应退役：%s %d", row.State, row.ConsecutiveFailures)
	}
	svc.SteamProbe = func(ctx context.Context, port int) (bool, int) { return true, 200 }
	svc.ExitIPProbe = func(ctx context.Context, port int) string { return "2.2.2.2" }
	if _, err := svc.testClashNodes(context.Background(), nil, true); err != nil {
		t.Fatal(err)
	}
	row = readLedgerRow(t, svc, configText, url, "节点A")
	if row.State != proxypool.NodeActive || row.ConsecutiveFailures != 0 {
		t.Fatalf("新成功证据应恢复退役节点：%s %d", row.State, row.ConsecutiveFailures)
	}
}

func TestLedgerAliveStatsFromCooldownSkipped(t *testing.T) {
	svc, _, _, _, _, configText, url := newTestFixture(t)
	now := fixedNow()
	seedLedgerNode(t, svc, configText, url, "节点A", proxypool.NodeActive,
		ledgerCooldown(now.Add(30*time.Minute)), ledgerLastL2(now), ledgerExitIP("9.9.9.9"))
	seedLedgerNode(t, svc, configText, url, "节点B", proxypool.NodeActive)
	svc.SteamProbe = failAllProbe
	svc.ExitIPProbe = failAllExitIP

	result, err := svc.testClashNodes(context.Background(), nil, false)
	if err != nil {
		t.Fatal(err)
	}
	if result.Alive != 1 {
		t.Fatalf("冷却跳过按账本存活应计入：alive=%d", result.Alive)
	}
	if result.AliveUnique != 1 {
		t.Fatalf("unique 应含账本出口 9.9.9.9：%d", result.AliveUnique)
	}
}

func TestSelectorSelfHealSwitchesToFastestAlive(t *testing.T) {
	svc, _, ctl, _, runtimePath, _, _ := newTestFixture(t)
	configRaw, _ := os.ReadFile(runtimePath)
	lanes, _ := clashcfg.ParseProbeLanes(string(configRaw))
	portA := lanes[0].Port
	svc.SteamProbe = func(ctx context.Context, port int) (bool, int) {
		if port == portA {
			return false, 502
		}
		return true, 200
	}
	result, err := svc.testClashNodes(context.Background(), nil, true)
	if err != nil {
		t.Fatal(err)
	}
	ctl.mu.Lock()
	last := ctl.putLog[len(ctl.putLog)-1]
	ctl.mu.Unlock()
	if last != "GLOBAL:节点B" {
		t.Fatalf("自愈应经控制器切最快健康节点 B：%q", last)
	}
	if result.Alive != 1 {
		t.Fatalf("存活统计应为 1：%d", result.Alive)
	}
}

func TestSelectorSelfHealNotTriggeredWhenCurrentAlive(t *testing.T) {
	svc, _, ctl, _, _, _, _ := newTestFixture(t)
	result, err := svc.testClashNodes(context.Background(), nil, true)
	if err != nil {
		t.Fatal(err)
	}
	ctl.mu.Lock()
	puts := append([]string{}, ctl.putLog...)
	ctl.mu.Unlock()
	for _, p := range puts {
		if strings.HasPrefix(p, "GLOBAL:") {
			t.Fatalf("当前选中存活不应触发自愈：%q", p)
		}
	}
	if result.Alive != 2 {
		t.Fatalf("两节点应都活：%d", result.Alive)
	}
}

func TestLaneProbeRejectedGroupProducesNoEvidence(t *testing.T) {
	svc, _, ctl, counts, _, _, _ := newTestFixture(t)
	ctl.mu.Lock()
	ctl.reject["HlProbeLane-1"] = true
	ctl.mu.Unlock()
	svc.ControllerFactory = func(baseURL, secret string) clashTestController { return ctl }

	result, err := svc.testClashNodes(context.Background(), nil, true)
	if err != nil {
		t.Fatal(err)
	}
	if result.Probed != 2 {
		t.Fatalf("结果集应含两行（含未探到的）：probed=%d", result.Probed)
	}
	probedTrue := 0
	for _, row := range result.Nodes {
		if row.Probed {
			probedTrue++
		}
	}
	if probedTrue != 1 {
		t.Fatalf("被拒组的那行不应产生健康证据：probed=true 行数=%d", probedTrue)
	}
	if counts.portsCount() != 1 {
		t.Fatalf("只有可用 lane 应真的发起探测：实际 %d 次", counts.portsCount())
	}
}

func TestNoLanesAndEntryDownFailsWithUserMessage(t *testing.T) {
	svc, kernel, _, _, _, _, _ := newTestFixture(t)
	svc.PortListeningFn = func(int) bool { return false }
	kernel.mu.Lock()
	kernel.st.EntryReady = false
	kernel.mu.Unlock()
	svc.TestStart(context.Background())
	snap := waitSessionPhase(t, svc, "failed", 3*time.Second)
	if snap.Error == nil || !strings.Contains(*snap.Error, "内核入口不可用") {
		t.Fatalf("lane 缺席+入口不可用应带用户文案：%v", snap.Error)
	}
	deprecated, _, _ := readSubRow(t, svc, 1)
	if deprecated {
		t.Fatalf("未探测不应污染账本触发废弃")
	}
}

func TestFirstCheckReuseWindow(t *testing.T) {
	svc, _, _, _, _, configText, url := newTestFixture(t)
	now := fixedNow()
	svc.Now = func() time.Time { return now }
	seedLedgerNode(t, svc, configText, url, "节点A", proxypool.NodeActive, ledgerLastL2(now.Add(-2*time.Minute)))
	seedLedgerNode(t, svc, configText, url, "节点B", proxypool.NodeActive, ledgerLastL2(now.Add(-2*time.Minute)))
	if !svc.ClashNodesFresh(context.Background(), 1) {
		t.Fatalf("窗口内（2 分钟前）应判定 fresh")
	}
	if svc.ClashNodesFresh(context.Background(), 2) {
		t.Fatalf("无账本订阅应判定不 fresh")
	}
	svc.Now = func() time.Time { return now.Add(6 * time.Minute) }
	if svc.ClashNodesFresh(context.Background(), 1) {
		t.Fatalf("窗口外应判定不 fresh")
	}
}

func TestFirstCheckSkipsInsideWindowAndProbesOutside(t *testing.T) {
	svc, _, _, counts, _, configText, url := newTestFixture(t)
	now := fixedNow()
	svc.Now = func() time.Time { return now }
	seedLedgerNode(t, svc, configText, url, "节点A", proxypool.NodeActive, ledgerLastL2(now))
	seedLedgerNode(t, svc, configText, url, "节点B", proxypool.NodeActive, ledgerLastL2(now))

	svc.SpawnFirstCheck(1, true)
	time.Sleep(150 * time.Millisecond)
	if counts.portsCount() != 0 {
		t.Fatalf("复用窗口内不应出网，实际 %d 次探针调用", counts.portsCount())
	}
	if snap, ok := svc.TestProgress(context.Background()).(map[string]any); !ok || snap["phase"] != "idle" {
		t.Fatalf("复用跳过不应建会话：%v", svc.TestProgress(context.Background()))
	}

	svc.Now = func() time.Time { return now.Add(10 * time.Minute) }
	svc.SpawnFirstCheck(1, true)
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) && counts.portsCount() < 2 {
		time.Sleep(5 * time.Millisecond)
	}
	if counts.portsCount() < 2 {
		t.Fatalf("窗口外首检应出网 ≥2 次，实际 %d", counts.portsCount())
	}
	waitSessionPhase(t, svc, "done", 3*time.Second)
}

func TestTestLockSerializesConcurrentRuns(t *testing.T) {
	svc, _, _, _, _, _, _ := newTestFixture(t)
	entered := make(chan struct{}, 1)
	release := make(chan struct{})
	svc.SteamProbe = func(ctx context.Context, port int) (bool, int) {
		select {
		case entered <- struct{}{}:
		default:
		}
		<-release
		return true, 200
	}
	svc.ExitIPProbe = func(ctx context.Context, port int) string { <-release; return "1.1.1.1" }

	doneManual := make(chan error, 1)
	go func() {
		_, err := svc.testClashNodes(context.Background(), nil, true)
		doneManual <- err
	}()
	<-entered

	secondDone := make(chan error, 1)
	go func() {
		_, err := svc.testClashNodes(context.Background(), nil, true)
		secondDone <- err
	}()
	select {
	case err := <-secondDone:
		t.Fatalf("持锁期间第二个检测不应进入 impl：%v", err)
	case <-time.After(150 * time.Millisecond):
	}
	close(release)
	if err := <-doneManual; err != nil {
		t.Fatal(err)
	}
	if err := <-secondDone; err != nil {
		t.Fatal(err)
	}
}
