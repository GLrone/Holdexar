package proxypool

// 编排层测试：步骤顺序（签名读点 / 同步 / 重建的相对次序）、失败隔离（一步失败
// 不拖垮其余）、准入晋升串联、出口聚合消费（fail-soft、无后续动作）；
// live 一轮真内核维护（沙箱节点拓扑照 health_live_test.go：socks5 回环节点 +
// 本地目标桩），覆盖同步 → 健康 → 签名变化 → 重建热重载全链。

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"holdexar/server-go/internal/clashcfg"
	"holdexar/server-go/internal/clashkernel"
	"holdexar/server-go/internal/store"
)

// newOrchestrateDB 全列订阅表 + 节点 / 快照 / 健康 / 出口账本 schema 的隔离测试库。
func newOrchestrateDB(t *testing.T) (*sql.DB, *store.WriteGate, string) {
	t.Helper()
	db, gate, dir := newTestDB(t)
	ctx := context.Background()
	conn := mustConn(t, db)
	if err := EnsureSchema(ctx, conn); err != nil {
		t.Fatal(err)
	}
	if err := EnsureHealthSchema(ctx, conn); err != nil {
		t.Fatal(err)
	}
	if err := EnsureExitStatsSchema(ctx, conn); err != nil {
		t.Fatal(err)
	}
	if err := conn.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`DROP TABLE proxy_subscriptions`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`CREATE TABLE proxy_subscriptions (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		url TEXT NOT NULL,
		kind VARCHAR(10) NOT NULL DEFAULT 'clash',
		admission_status VARCHAR(16) NOT NULL DEFAULT 'CANDIDATE',
		last_stats JSON,
		last_fetch_at DATETIME, last_fetch_status VARCHAR(32),
		last_success_at DATETIME, last_error VARCHAR(500), cache_url_identity VARCHAR(64),
		auto_refresh BOOLEAN NOT NULL DEFAULT 1)`); err != nil {
		t.Fatal(err)
	}
	return db, gate, dir
}

// deadOrchestrateDB 已关闭的隔离库（制造「数据库不可用」的失败夹具）。
func deadOrchestrateDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := store.Open(filepath.Join(t.TempDir(), "dead.db"))
	if err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	return db
}

// eventLog 步骤事件序（跨注入点共享的调用顺序记录）。
type eventLog struct {
	mu    sync.Mutex
	items []string
}

func (l *eventLog) add(name string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.items = append(l.items, name)
}

func (l *eventLog) joined() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return strings.Join(l.items, ",")
}

// signatureStub 池签名假读点：按调用次序返回预置序列（越界复读末值），可把每次
// 读取记入事件序。
type signatureStub struct {
	values [][]string
	calls  int
	events *eventLog
}

func (s *signatureStub) read(context.Context) ([]string, error) {
	if s.events != nil {
		s.events.add("signature")
	}
	i := s.calls
	s.calls++
	if i >= len(s.values) {
		i = len(s.values) - 1
	}
	return s.values[i], nil
}

// stubSubBodyAt 逐节点独立桩端口：沙箱节点只有 type/server/port 三键，端口
// 参与节点身份（PortIsIdentity），同口两节点会在 Registry 折叠成一行。
func stubSubBodyAt(ports []int, names []string) string {
	var b strings.Builder
	b.WriteString("proxies:\n")
	for i, name := range names {
		fmt.Fprintf(&b, "  - {name: %s, type: socks5, server: 127.0.0.1, port: %d}\n", name, ports[i])
	}
	return b.String()
}

func stubFetchResult(body string) *FetchResult {
	return &FetchResult{
		Raw: []byte(body), HTTPStatus: 200, ContentType: "text/yaml",
		Channel: ChannelDirect, Fmt: FormatYAML, DurationMS: 10,
		Attempts: []FetchAttempt{{Channel: ChannelDirect, OK: true, Fmt: FormatYAML, Stage: "complete", EgressID: ChannelDirect}},
	}
}

// seedOrchestrateSubscription 播种一条订阅（密文 URL，默认 CANDIDATE）。
func seedOrchestrateSubscription(t *testing.T, db *sql.DB, dataDir, plainURL string) int64 {
	t.Helper()
	sealed, err := SealURL(dataDir, plainURL)
	if err != nil {
		t.Fatal(err)
	}
	res, err := db.Exec(`INSERT INTO proxy_subscriptions (url) VALUES (?)`, sealed)
	if err != nil {
		t.Fatal(err)
	}
	id, _ := res.LastInsertId()
	return id
}

// TestRunPoolMaintenanceStepOrder 步骤顺序：before 签名 → 同步 → 准入 → 健康跳过
// → after 签名 → 签名变化触发重建。
func TestRunPoolMaintenanceStepOrder(t *testing.T) {
	db, gate, dir := newOrchestrateDB(t)
	ctx := context.Background()
	seedOrchestrateSubscription(t, db, dir, "https://provider.example.com/order-sub")

	events := &eventLog{}
	sig := &signatureStub{values: [][]string{{"a"}, {"a", "b"}}, events: events}
	o := &Orchestrator{
		DB: db, Gate: gate, DataDir: dir,
		Syncer: &Syncer{
			DB: db, Gate: gate, DataDir: dir, Now: nowFixed,
			Fetch: func(ctx context.Context, url string, channels []Channel, opts FetchOptions) (*FetchResult, error) {
				events.add("sync")
				return stubFetchResult(stubSubBodyAt([]int{0, 0}, []string{"N1", "N2"})), nil
			},
		},
		Signature: sig.read,
		Rebuild: func(ctx context.Context) error {
			events.add("rebuild")
			return nil
		},
	}
	report, err := RunPoolMaintenance(ctx, o)
	if err != nil {
		t.Fatal(err)
	}
	if want := "signature,sync,signature,rebuild"; events.joined() != want {
		t.Fatalf("步骤顺序应为 %s，实际 %s", want, events.joined())
	}
	if !report.Sync.Ran || report.Sync.Error != "" {
		t.Fatalf("同步应成功执行：%+v", report.Sync)
	}
	if report.SyncResult == nil || len(report.SyncResult.SnapshotSha256) != 1 {
		t.Fatalf("同步应落一个订阅快照：%+v", report.SyncResult)
	}
	if !report.Admission.Ran || report.Admission.Error != "" {
		t.Fatalf("无候选订阅时准入应空转不报错：%+v", report.Admission)
	}
	if report.Health.Ran {
		t.Fatalf("Health 未注入应跳过：%+v", report.Health)
	}
	if !report.SignatureChanged || !report.Rebuild.Ran || report.Rebuild.Error != "" {
		t.Fatalf("签名变化应触发重建：%+v", report)
	}
}

// TestRunPoolMaintenanceAdmissionPromotes 体检有可用节点的候选订阅自动转正。
func TestRunPoolMaintenanceAdmissionPromotes(t *testing.T) {
	db, gate, dir := newOrchestrateDB(t)
	ctx := context.Background()
	seedOrchestrateSubscription(t, db, dir, "https://provider.example.com/admit-sub")
	sig := &signatureStub{values: [][]string{{"a"}, {"a"}}}
	o := &Orchestrator{
		DB: db, Gate: gate, DataDir: dir,
		Syncer: &Syncer{
			DB: db, Gate: gate, DataDir: dir, Now: nowFixed,
			Fetch: func(ctx context.Context, url string, channels []Channel, opts FetchOptions) (*FetchResult, error) {
				return stubFetchResult(stubSubBodyAt([]int{0, 0}, []string{"N1", "N2"})), nil
			},
		},
		Signature: sig.read,
	}
	if _, err := RunPoolMaintenance(ctx, o); err != nil {
		t.Fatal(err)
	}
	subID := queryInt(t, db, `SELECT id FROM proxy_subscriptions LIMIT 1`)
	if _, err := db.Exec(`UPDATE proxy_subscriptions SET last_stats = '{"alive":2}' WHERE id = ?`, subID); err != nil {
		t.Fatal(err)
	}
	report, err := RunPoolMaintenance(ctx, o)
	if err != nil {
		t.Fatal(err)
	}
	if report.Promoted != 1 || report.Admission.Error != "" {
		t.Fatalf("体检有可用节点的候选应转正：%+v", report.Admission)
	}
	if status := mustStatus(t, db, subID); status != AdmissionActive {
		t.Fatalf("准入状态应为 ACTIVE，实际 %s", status)
	}
}

// TestRunPoolMaintenanceSyncFailureIsolation 同步失败不拖垮准入与重建判定。
func TestRunPoolMaintenanceSyncFailureIsolation(t *testing.T) {
	db, gate, dir := newOrchestrateDB(t)
	ctx := context.Background()
	// 订阅表缺失 → 同步的清单读取整体失败（比单订阅下载失败更深的顶层失败）
	if _, err := db.Exec(`DROP TABLE proxy_subscriptions`); err != nil {
		t.Fatal(err)
	}
	sig := &signatureStub{values: [][]string{{"a"}, {"a", "b"}}}
	o := &Orchestrator{
		DB: db, Gate: gate, DataDir: dir,
		Syncer: &Syncer{DB: db, Gate: gate, DataDir: dir, Now: nowFixed,
			Fetch: func(ctx context.Context, url string, channels []Channel, opts FetchOptions) (*FetchResult, error) {
				return stubFetchResult(stubSubBodyAt([]int{0}, []string{"N1"})), nil
			}},
		Signature: sig.read,
		Rebuild:   func(ctx context.Context) error { return nil },
	}
	report, err := RunPoolMaintenance(ctx, o)
	if err != nil {
		t.Fatalf("同步失败不得整体失败：%v", err)
	}
	if !report.Sync.Ran || report.Sync.Error == "" {
		t.Fatalf("同步失败应留痕：%+v", report.Sync)
	}
	if report.Admission.Error == "" {
		t.Fatalf("订阅表缺失时准入也应留痕：%+v", report.Admission)
	}
	if !report.SignatureChanged || !report.Rebuild.Ran {
		t.Fatalf("失败隔离后重建判定照常：%+v", report)
	}
}

// TestRunPoolMaintenanceHealthWaveFailure 探针层失败只进收尾 end_reason，波次
// 照常建行收尾，编排继续走到签名比对。
func TestRunPoolMaintenanceHealthWaveFailure(t *testing.T) {
	db, gate, dir := newOrchestrateDB(t)
	ctx := context.Background()
	// 池文件给一个节点名，让波次真的走进节点读取（读已关闭的库即失败）
	if err := os.MkdirAll(filepath.Dir(PoolPath(dir)), 0o755); err != nil {
		t.Fatal(err)
	}
	pool := "proxies:\n  - name: Pool-01\n    type: ss\n    server: a.example.com\n    port: 443\n"
	if err := os.WriteFile(PoolPath(dir), []byte(pool), 0o644); err != nil {
		t.Fatal(err)
	}
	checker := &HealthChecker{DB: deadOrchestrateDB(t), Gate: gate, DataDir: dir,
		ConfigPath: filepath.Join(dir, "crawl-runtime.yaml")}
	sig := &signatureStub{values: [][]string{{"a"}, {"a"}}}
	o := &Orchestrator{DB: db, Gate: gate, DataDir: dir, Health: checker, Signature: sig.read}
	report, err := RunPoolMaintenance(ctx, o)
	if err != nil {
		t.Fatal(err)
	}
	if !report.Health.Ran || report.HealthRunID == 0 {
		t.Fatalf("健康波次应建运行行：%+v", report.Health)
	}
	if report.HealthSummary.EndReason != "l1_error,l2_error" {
		t.Fatalf("探针层失败应进收尾原因，实际 %q", report.HealthSummary.EndReason)
	}
	if report.Health.Error != "" {
		t.Fatalf("探针层失败不得升级为波次失败：%+v", report.Health)
	}
	if finished := queryString(t, db, `SELECT CAST(finished_at AS TEXT) FROM health_runs WHERE id = ?`, report.HealthRunID); !finished.Valid || finished.String == "" {
		t.Fatalf("运行行应收尾（finished_at 非空）：%v", finished)
	}
	if report.SignatureChanged || report.Rebuild.Ran {
		t.Fatalf("签名未变不得触发重建：%+v", report)
	}
}

// TestRunPoolMaintenanceSignatureUnreadable 签名读不了时跳过重建判定，不扰动内核。
func TestRunPoolMaintenanceSignatureUnreadable(t *testing.T) {
	db, gate, dir := newOrchestrateDB(t)
	ctx := context.Background()
	o := &Orchestrator{DB: db, Gate: gate, DataDir: dir,
		Signature: func(ctx context.Context) ([]string, error) {
			return nil, fmt.Errorf("库打不开")
		},
		Rebuild: func(ctx context.Context) error {
			t.Fatal("签名读不了不得触发重建")
			return nil
		},
	}
	report, err := RunPoolMaintenance(ctx, o)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(report.Rebuild.Error, "重建判定跳过") {
		t.Fatalf("应记跳过原因：%+v", report.Rebuild)
	}
}

// TestRunDeadProbeSweepOrder 恢复扫批：空批不炸；签名变化触发重建；runner 失败
// 留痕不上抛。
func TestRunDeadProbeSweepOrder(t *testing.T) {
	db, gate, dir := newOrchestrateDB(t)
	ctx := context.Background()
	sig := &signatureStub{values: [][]string{{"a"}, {"a"}}}
	o := &Orchestrator{DB: db, Gate: gate, DataDir: dir, Signature: sig.read}
	report, err := RunDeadProbeSweep(ctx, o, "")
	if err != nil {
		t.Fatal(err)
	}
	if len(report.Outcomes) != 0 || report.Recovered != 0 {
		t.Fatalf("无待恢复节点应空转：%+v", report)
	}
	if report.SignatureChanged || report.Rebuild.Ran {
		t.Fatalf("签名未变不得触发重建：%+v", report)
	}

	sig2 := &signatureStub{values: [][]string{{"a"}, {"a", "b"}}}
	o2 := &Orchestrator{DB: db, Gate: gate, DataDir: dir, Signature: sig2.read,
		Rebuild: func(ctx context.Context) error { return nil }}
	rebuilt, err := RunDeadProbeSweep(ctx, o2, "")
	if err != nil {
		t.Fatal(err)
	}
	if !rebuilt.SignatureChanged || !rebuilt.Rebuild.Ran {
		t.Fatalf("恢复使合格集变化时应触发重建：%+v", rebuilt)
	}

	o3 := &Orchestrator{DB: db, Gate: gate, DataDir: dir,
		Prober: &DeadProbeRunner{DB: deadOrchestrateDB(t), Gate: gate, DataDir: dir}}
	sweep, err := RunDeadProbeSweep(ctx, o3, "")
	if err != nil {
		t.Fatalf("runner 失败不得整体失败：%v", err)
	}
	if !strings.HasPrefix(sweep.Rebuild.Error, "恢复探测失败") {
		t.Fatalf("runner 失败应留痕：%+v", sweep.Rebuild)
	}
}

// TestIngestCrawlExits 出口聚合消费：runID=0 拒收、正常落行、写失败 fail-soft、
// 无重建后续动作（对齐 Python write_run_exits → record_run_exits）。
func TestIngestCrawlExits(t *testing.T) {
	db, gate, dir := newOrchestrateDB(t)
	ctx := context.Background()
	o := &Orchestrator{DB: db, Gate: gate, DataDir: dir}

	n, err := IngestCrawlExits(ctx, o, 0, []ExitStatsRow{{ExitIP: "1.2.3.4", Endpoint: "browse", Success: 1}}, nil)
	if err != nil || n != 0 {
		t.Fatalf("无作业身份的出口行应拒收：n=%d err=%v", n, err)
	}

	rows := []ExitStatsRow{
		{ExitIP: "1.2.3.4", Endpoint: "browse", Requests: 3, Success: 2, E429: 1},
		{ExitIP: "5.6.7.8", Endpoint: "browse", Requests: 1, Timeout: 1},
	}
	n, err = IngestCrawlExits(ctx, o, 77, rows, map[string]string{"1.2.3.4": "Pool-01"})
	if err != nil || n != 2 {
		t.Fatalf("出口聚合应落 2 行：n=%d err=%v", n, err)
	}
	if got := queryInt(t, db, `SELECT COUNT(*) FROM proxy_run_exits WHERE run_id = 77`); got != 2 {
		t.Fatalf("出口账本应 2 行，实际 %d", got)
	}
	if node := queryString(t, db, `SELECT node FROM proxy_run_exits WHERE exit_ip = '1.2.3.4'`); !node.Valid || node.String != "Pool-01" {
		t.Fatalf("出口到节点的映射应落账：%v", node)
	}

	deadDB := deadOrchestrateDB(t)
	o2 := &Orchestrator{DB: deadDB, Gate: store.NewWriteGate(deadDB, filepath.Join(t.TempDir(), "dead.db")), DataDir: dir}
	if _, err := IngestCrawlExits(ctx, o2, 77, rows, nil); err == nil {
		t.Fatalf("写失败应返回错误交调用方降级")
	}
}

// ── live：真内核一轮维护编排 ─────────────────────────────────────────────────

func TestLiveOrchestrationMaintenanceRound(t *testing.T) {
	exe := liveKernelExe(t)
	ctx := context.Background()
	target := newLiveProbeTarget(t)
	// 每节点独立桩端口：沙箱节点只有 type/server/port 三键，端口参与节点身份
	// （PortIsIdentity），同口两节点会在 Registry 折叠成一行。
	socksPorts := []int{minimalSocks5Server(t), minimalSocks5Server(t), minimalSocks5Server(t)}

	db, gate, dataDir := newOrchestrateDB(t)
	sealed, err := SealURL(dataDir, "https://provider.example.com/live-sub")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO proxy_subscriptions (url, kind, admission_status, auto_refresh)
		VALUES (?, 'clash', 'ACTIVE', 1)`, sealed); err != nil {
		t.Fatal(err)
	}

	var subBody atomic.Value
	subBody.Store(stubSubBodyAt(socksPorts[:2], []string{"Live-01", "Live-02"}))
	syncer := &Syncer{DB: db, Gate: gate, DataDir: dataDir,
		Fetch: func(ctx context.Context, url string, channels []Channel, opts FetchOptions) (*FetchResult, error) {
			return stubFetchResult(subBody.Load().(string)), nil
		}}

	// bootstrap：同步 → 建池 → 冷启动内核（编排第一轮开始时内核已在跑）
	if _, err := syncer.SyncSubscriptions(ctx); err != nil {
		t.Fatal(err)
	}
	if err := gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		if err := EnsureSchema(ctx, conn); err != nil {
			return err
		}
		_, err := BuildPool(ctx, conn, dataDir, time.Now())
		return err
	}); err != nil {
		t.Fatal(err)
	}
	var runtime clashkernel.Runtime
	configPath, _, err := clashcfg.PrepareRuntimeConfig(dataDir, 0, false)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := runtime.Start(exe, configPath, clashkernel.StartOptions{}); err != nil {
		t.Fatalf("真内核启动失败：%v", err)
	}
	t.Cleanup(func() { runtime.Stop() })

	checker := &HealthChecker{DB: db, Gate: gate, DataDir: dataDir,
		ControllerURL: runtime.ControllerURL, Secret: runtime.Secret, ConfigPath: configPath}
	kernelRebuild := KernelRebuildFunc(gate, dataDir, exe, &runtime, 0)
	rebuildCalls := 0
	o := &Orchestrator{
		DB: db, Gate: gate, DataDir: dataDir, Syncer: syncer, Health: checker,
		ProbeL1URL: target.server.URL + "/ip",
		ProbeL2URL: target.server.URL + "/IStoreBrowseService/GetItems/v1/",
		Rebuild: func(ctx context.Context) error {
			rebuildCalls++
			return kernelRebuild(ctx)
		},
	}

	pid0 := runtime.ProcessPid()

	// 第一轮：同步幂等、健康波次真探（回环沙箱全通）、签名不变、不重建
	r1, err := RunPoolMaintenance(ctx, o)
	if err != nil {
		t.Fatal(err)
	}
	if r1.Sync.Error != "" || r1.Admission.Error != "" || r1.Health.Error != "" {
		t.Fatalf("第一轮不应有失败：%+v", r1)
	}
	if r1.HealthSummary.Total != 2 || r1.HealthSummary.SteamOK != 2 || r1.HealthSummary.IPKnown != 2 {
		t.Fatalf("回环沙箱两节点应全通：%+v", r1.HealthSummary)
	}
	if r1.SignatureChanged || r1.Rebuild.Ran || rebuildCalls != 0 {
		t.Fatalf("签名未变不得触发重建：%+v calls=%d", r1, rebuildCalls)
	}
	if got := queryInt(t, db, `SELECT COUNT(*) FROM proxy_nodes WHERE state = 'ACTIVE'`); got != 2 {
		t.Fatalf("健康波次应晋升两节点，实际 %d", got)
	}
	if finished := queryString(t, db, `SELECT CAST(finished_at AS TEXT) FROM health_runs WHERE id = ?`, r1.HealthRunID); !finished.Valid || finished.String == "" {
		t.Fatalf("运行行应收尾：%v", finished)
	}

	// 第二轮：订阅带进新节点 → 签名变化 → 重建（真热重载：进程不动、池跟上）
	subBody.Store(stubSubBodyAt(socksPorts, []string{"Live-01", "Live-02", "Live-03"}))
	r2, err := RunPoolMaintenance(ctx, o)
	if err != nil {
		t.Fatal(err)
	}
	if !r2.SignatureChanged || !r2.Rebuild.Ran || r2.Rebuild.Error != "" {
		t.Fatalf("新节点应触发重建：%+v", r2)
	}
	if rebuildCalls != 1 {
		t.Fatalf("重建回调应恰好调用一次，实际 %d", rebuildCalls)
	}
	names := PoolFileNames(dataDir)
	if len(names) != 3 {
		t.Fatalf("重建后池文件应 3 节点，实际 %v", names)
	}
	if pid := runtime.ProcessPid(); pid != pid0 || pid == 0 {
		t.Fatalf("重建应为热重载（进程不动）：pid %d → %d", pid0, pid)
	}
	if !runtime.Status().Running || !runtime.EntryListening() {
		t.Fatalf("重建后内核应仍在跑且入口在听：%+v", runtime.Status())
	}

	// 恢复探测扫批（无待恢复节点空转）与出口聚合消费（真落账）
	sweep, err := RunDeadProbeSweep(ctx, o, "")
	if err != nil {
		t.Fatal(err)
	}
	if len(sweep.Outcomes) != 0 || sweep.Rebuild.Error != "" {
		t.Fatalf("无待恢复节点应空转不报错：%+v", sweep)
	}
	n, err := IngestCrawlExits(ctx, o, 901,
		[]ExitStatsRow{{ExitIP: "127.0.0.1", Endpoint: "browse", Requests: 3, Success: 3, DurationMS: 120}},
		map[string]string{"127.0.0.1": "Live-01"})
	if err != nil || n != 1 {
		t.Fatalf("出口聚合应落 1 行：n=%d err=%v", n, err)
	}
	if got := queryInt(t, db, `SELECT COUNT(*) FROM proxy_run_exits WHERE run_id = 901`); got != 1 {
		t.Fatalf("出口账本应 1 行，实际 %d", got)
	}
	t.Logf("live 编排验收通过：一轮维护 + 签名变化热重载 + 扫批 + 出口聚合")
}
