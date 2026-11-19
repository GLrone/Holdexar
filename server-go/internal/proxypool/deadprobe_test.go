package proxypool

import (
	"context"
	"database/sql"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	"holdexar/server-go/internal/clashkernel"
	"holdexar/server-go/internal/store"
)

func deadTestDB(t *testing.T) (*sql.DB, *store.WriteGate, string) {
	t.Helper()
	db, gate, dir := newTestDB(t)
	if err := EnsureSchema(context.Background(), mustConn(t, db)); err != nil {
		t.Fatal(err)
	}
	if err := EnsureHealthSchema(context.Background(), mustConn(t, db)); err != nil {
		t.Fatal(err)
	}
	return db, gate, dir
}

func TestFreeProbePortScansSegment(t *testing.T) {
	listen := func(t *testing.T, port int) net.Listener {
		t.Helper()
		l, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", port))
		if err != nil {
			t.Fatalf("占口 %d 失败：%v", port, err)
		}
		t.Cleanup(func() { l.Close() })
		return l
	}
	base := freeProbePort(24300)
	if base != 24300 {
		t.Fatalf("空段应返回首选口 24300，实际 %d", base)
	}
	listen(t, 24300)
	if got := freeProbePort(24300); got != 24301 {
		t.Fatalf("首选口被占应顺延 24301，实际 %d", got)
	}
	listen(t, 24301)
	listen(t, 24302)
	listen(t, 24303)
	listen(t, 24304)
	listen(t, 24305)
	listen(t, 24306)
	listen(t, 24307)
	if got := freeProbePort(24300); got != 0 {
		t.Fatalf("整段占满应返回 0，实际 %d", got)
	}
}

func TestDeadProbeBundlesAttribution(t *testing.T) {
	sockConfig := func() map[string]any {
		return map[string]any{"name": "香港 01", "type": "socks5",
			"server": "127.0.0.1", "port": int64(23001)}
	}
	target := RecoveredNode{NodeID: "fp-a", RuntimeName: "1|香港 01", OriginalName: "香港 01",
		Config: sockConfig(), SubscriptionID: sql.NullInt64{Int64: 1, Valid: true}}

	peer := target
	peer.NodeID = "fp-peer"
	peer.RuntimeName = "1|香港 02"
	peer.OriginalName = "香港 02"
	ready, bundle, rejected := deadProbeBundles([]RecoveredNode{target}, []RecoveredNode{peer}, nil, nil)
	if len(rejected) != 0 || len(ready) != 1 {
		t.Fatalf("完整配置应就绪：%v / %v", ready, rejected)
	}
	if len(bundle.proxies) != 1 {
		t.Fatalf("产物应只有根节点一条，实际 %v", bundle.proxies)
	}

	broken := target
	broken.NodeID = "fp-b"
	broken.RuntimeName = "1|坏配置"
	broken.OriginalName = "坏配置"
	broken.Config = map[string]any{"type": "ss", "port": int64(1)}
	_, _, rejected = deadProbeBundles([]RecoveredNode{target, broken}, nil, nil, nil)
	if len(rejected) != 1 {
		t.Fatalf("缺 server 应拒绝一条，实际 %v", rejected)
	}
	if rejected[0].Cause != CauseDependency || rejected[0].NodeFailure {
		t.Fatalf("依赖归因不对：%+v", rejected[0])
	}
	if rejected[0].Detail == "" || !contains(rejected[0].Detail, "节点依赖不可用") {
		t.Fatalf("依赖细节缺失：%q", rejected[0].Detail)
	}

	dp := target
	dp.NodeID = "fp-c"
	dp.RuntimeName = "1|缺依赖"
	dp.OriginalName = "缺依赖"
	dp.Config["dialer-proxy"] = "幽灵依赖"
	_, _, rejected = deadProbeBundles([]RecoveredNode{dp}, nil,
		map[int64]map[string]any{1: {}}, map[int64]string{1: "订阅快照不可恢复"})
	if len(rejected) != 1 || rejected[0].Cause != CauseDependency {
		t.Fatalf("缺依赖应归因 dependency：%v", rejected)
	}
	if !contains(rejected[0].Detail, "订阅快照不可恢复") {
		t.Fatalf("来源错误应并入细节：%q", rejected[0].Detail)
	}
}

func TestProbeOutcomeFromResultAttribution(t *testing.T) {
	node := RecoveredNode{NodeID: "fp-a", RuntimeName: "1|A"}
	ok := probeOutcomeFromResult(node, ProbeResult{RuntimeName: "1|A", OK: true, DelayMS: 120, Valid: true}).Outcome
	if !ok.OK || !ok.NodeFailure || ok.Cause != CauseNode || ok.DelayMS != 120 {
		t.Fatalf("成功探测应归因 node：%+v", ok)
	}
	fail := probeOutcomeFromResult(node, ProbeResult{RuntimeName: "1|A", Valid: true}).Outcome
	if fail.OK || !fail.NodeFailure || fail.Cause != CauseNode {
		t.Fatalf("真实失败应归因 node（节点失败证据）：%+v", fail)
	}
	invalid := probeOutcomeFromResult(node, ProbeResult{RuntimeName: "1|A"}).Outcome
	if invalid.OK || invalid.NodeFailure || invalid.Cause != CauseController {
		t.Fatalf("没测到节点不得构成节点证据：%+v", invalid)
	}
}

func TestLoadRecoverableNodesRotation(t *testing.T) {
	db, _, _ := deadTestDB(t)
	ctx := context.Background()
	now := time.Now()
	seedNode(t, db, "无记录", NodeDead, 0, int64(1))
	seedNode(t, db, "旧档", NodeDead, 0, int64(1))
	seedNode(t, db, "新档", NodeRetired, 0, int64(1))
	seedNode(t, db, "在池", NodeActive, 0, int64(1))
	for _, row := range []struct {
		name string
		at   time.Time
	}{
		{"旧档", now.Add(-2 * time.Hour)},
		{"新档", now.Add(-1 * time.Hour)},
	} {
		if _, err := db.Exec(`INSERT INTO health_observations
			(node_id, level, ok, observed_at, channel) VALUES (?, 'L2', 1, ?, 'scheduled')`,
			"fp-"+row.name, pyTimeText(row.at)); err != nil {
			t.Fatal(err)
		}
	}
	conn := mustConn(t, db)
	nodes, err := loadRecoverableNodes(ctx, conn, 24)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, n := range nodes {
		names = append(names, n.RuntimeName)
	}
	want := []string{"无记录", "旧档", "新档"}
	if len(names) != len(want) {
		t.Fatalf("应取 %v，实际 %v", want, names)
	}
	for i := range want {
		if names[i] != want[i] {
			t.Fatalf("轮转序应 %v，实际 %v", want, names)
		}
	}
	limited, err := loadRecoverableNodes(ctx, conn, 1)
	if err != nil || len(limited) != 1 || limited[0].RuntimeName != "无记录" {
		t.Fatalf("batch 应截断取批：%v %v", limited, err)
	}
	if nodes[0].OriginalName != "无记录" {
		t.Fatalf("原名缺省应回落运行名：%q", nodes[0].OriginalName)
	}
}

func TestRecoverDeadNodesKernelMissing(t *testing.T) {
	db, gate, dir := deadTestDB(t)
	seedNode(t, db, "n1", NodeDead, 0, int64(1))
	runner := &DeadProbeRunner{DB: db, Gate: gate, DataDir: dir}
	outcomes, err := runner.RecoverDeadNodes(context.Background(), "http://target/204", 24)
	if err != nil {
		t.Fatal(err)
	}
	if outcomes != nil {
		t.Fatalf("内核缺失应静默跳过，实际 %v", outcomes)
	}
}

func TestRecoverDeadNodesSourcelessAttribution(t *testing.T) {
	db, gate, dir := deadTestDB(t)
	seedNode(t, db, "无主", NodeDead, 1, nil)
	seedNode(t, db, "有主", NodeDead, 1, int64(1))
	exeDir := filepath.Join(dir, "clash")
	if err := os.MkdirAll(exeDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(exeDir, clashkernel.KernelFilename()), []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}
	runner := &DeadProbeRunner{DB: db, Gate: gate, DataDir: dir}
	outcomes, err := runner.RecoverDeadNodes(context.Background(), "http://target/204", 24)
	if err != nil {
		t.Fatal(err)
	}
	if len(outcomes) != 2 {
		t.Fatalf("应两条结论，实际 %v", outcomes)
	}
	byName := map[string]DeadProbeOutcome{}
	for _, o := range outcomes {
		byName[o.RuntimeName] = o
	}
	sourceless := byName["无主"]
	if sourceless.NodeID != "fp-无主" || sourceless.OK || sourceless.NodeFailure ||
		sourceless.Cause != CauseDependency || sourceless.Detail != "节点来源不存在" {
		t.Fatalf("无主节点应依赖归因：%+v", sourceless)
	}
	owned := byName["有主"]
	if owned.OK || owned.NodeFailure {
		t.Fatalf("占位内核探测不得构成节点证据：%+v", owned)
	}
	var state string
	var failures int64
	if err := db.QueryRow(`SELECT state, consecutive_failures FROM proxy_nodes WHERE runtime_name = '无主'`).
		Scan(&state, &failures); err != nil {
		t.Fatal(err)
	}
	if state != NodeDead || failures != 1 {
		t.Fatalf("依赖失败不得推进状态：%s %d", state, failures)
	}
	var obsOK int64
	var detail, channel sql.NullString
	if err := db.QueryRow(`SELECT ok, detail, channel FROM health_observations
		WHERE node_id = 'fp-无主' AND level = 'L2'`).Scan(&obsOK, &detail, &channel); err != nil {
		t.Fatal(err)
	}
	if obsOK != 0 || detail.String != "节点来源不存在" || channel.String != DefaultScheduleChannel {
		t.Fatalf("观测行形状不对：ok=%d detail=%q channel=%q", obsOK, detail.String, channel.String)
	}
	entries, _ := os.ReadDir(filepath.Join(dir, "proxypool", "_dead_probe"))
	if len(entries) != 0 {
		t.Fatalf("探测工作目录应清理，残留 %d 项", len(entries))
	}
}

func TestCommitRecoveryAdvancesStates(t *testing.T) {
	db, gate, dir := deadTestDB(t)
	seedNode(t, db, "复活的", NodeDead, 2, int64(1))
	seedNode(t, db, "再失败的", NodeStale, 1, int64(1))
	seedNode(t, db, "没测到的", NodeActive, 0, int64(1))
	runner := &DeadProbeRunner{DB: db, Gate: gate, DataDir: dir, Now: func() time.Time {
		return time.Unix(1800000000, 0)
	}}
	now := time.Unix(1800000000, 0)
	outcomes := []DeadProbeOutcome{
		{NodeID: "fp-复活的", RuntimeName: "复活的", OK: true, DelayMS: 130, Cause: CauseNode},
		{NodeID: "fp-再失败的", RuntimeName: "再失败的", OK: false, NodeFailure: true, Cause: CauseNode},
		{NodeID: "fp-没测到的", RuntimeName: "没测到的", OK: false, Cause: CauseController,
			Detail: "临时内核控制器未就绪"},
	}
	if err := runner.commitRecovery(context.Background(), outcomes, now, "http://target/204"); err != nil {
		t.Fatal(err)
	}
	var state string
	var failures int64
	var lastL2 sql.NullString
	if err := db.QueryRow(`SELECT state, consecutive_failures, CAST(last_l2_at AS TEXT)
		FROM proxy_nodes WHERE runtime_name = '复活的'`).Scan(&state, &failures, &lastL2); err != nil {
		t.Fatal(err)
	}
	if state != NodeActive || failures != 0 || lastL2.String != pyTimeText(now) {
		t.Fatalf("恢复成功应复活清零：%s %d %v", state, failures, lastL2)
	}
	if err := db.QueryRow(`SELECT state, consecutive_failures FROM proxy_nodes WHERE runtime_name = '再失败的'`).
		Scan(&state, &failures); err != nil {
		t.Fatal(err)
	}
	if state != NodeDead || failures != 2 {
		t.Fatalf("节点失败证据应计数推进：%s %d", state, failures)
	}
	if err := db.QueryRow(`SELECT state, consecutive_failures FROM proxy_nodes WHERE runtime_name = '没测到的'`).
		Scan(&state, &failures); err != nil {
		t.Fatal(err)
	}
	if state != NodeActive || failures != 0 {
		t.Fatalf("控制器失败不得罚节点：%s %d", state, failures)
	}
	var ok int64
	var detail, target sql.NullString
	var runID sql.NullInt64
	var latency sql.NullInt64
	if err := db.QueryRow(`SELECT ok, detail, run_id, target, latency_ms FROM health_observations
		WHERE node_id = 'fp-复活的' AND level = 'L2'`).Scan(&ok, &detail, &runID, &target, &latency); err != nil {
		t.Fatal(err)
	}
	if ok != 1 || detail.String != DetailRecoveryOK || runID.Valid ||
		target.String != "http://target/204" || latency.Int64 != 130 {
		t.Fatalf("恢复观测行形状不对：ok=%d detail=%q run=%v target=%q ms=%d",
			ok, detail.String, runID, target.String, latency.Int64)
	}
}

func TestRenderDeadProbeConfigShape(t *testing.T) {
	targets := []RecoveredNode{
		{NodeID: "a", RuntimeName: "1|A", OriginalName: "A",
			Config:         map[string]any{"name": "A", "type": "socks5", "server": "127.0.0.1", "port": int64(23001)},
			SubscriptionID: sql.NullInt64{Int64: 1, Valid: true}},
		{NodeID: "b", RuntimeName: "1|B", OriginalName: "B",
			Config:         map[string]any{"name": "B", "type": "socks5", "server": "127.0.0.1", "port": int64(23002)},
			SubscriptionID: sql.NullInt64{Int64: 1, Valid: true}},
	}
	bundle := deadProbeBundle{proxies: []any{
		map[string]any{"name": "1|A", "type": "socks5", "server": "127.0.0.1", "port": int64(23001)},
		map[string]any{"name": "1|B", "type": "socks5", "server": "127.0.0.1", "port": int64(23002)},
	}}
	text, err := renderDeadProbeConfig(targets, 23200, "s3cret", 23220, bundle)
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{
		"external-controller: 127.0.0.1:23200",
		"port: 23220", "port: 23221", "proxy: '1|A'", "proxy: '1|B'",
		"MATCH,DIRECT", "secret: s3cret",
	} {
		if !contains(text, want) {
			t.Fatalf("配置缺 %q：\n%s", want, text)
		}
	}
	if contains(text, "'23001'") || contains(text, "23001.0") {
		t.Fatalf("端口不得渲染成非整数形态：\n%s", text)
	}
}
