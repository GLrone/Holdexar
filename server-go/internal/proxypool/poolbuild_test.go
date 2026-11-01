package proxypool

// 池构建与重建钩子测试：合格集门禁、签名敏感性、构建三门禁、变化检测钩子。

import (
	"context"
	"database/sql"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
)

// poolTestDB 在既有夹具上补齐节点表与准入判定所需列。
func poolTestDB(t *testing.T) (*sql.DB, *store.WriteGate, string) {
	t.Helper()
	db, gate, dir := newTestDB(t)
	if err := EnsureSchema(context.Background(), mustConn(t, db)); err != nil {
		t.Fatal(err)
	}
	for _, ddl := range []string{
		`ALTER TABLE proxy_subscriptions ADD COLUMN admission_status VARCHAR(32)`,
		`ALTER TABLE proxy_subscriptions ADD COLUMN last_stats TEXT`,
	} {
		if _, err := db.Exec(ddl); err != nil {
			t.Fatal(err)
		}
	}
	return db, gate, dir
}

func seedPoolSubscription(t *testing.T, db *sql.DB, id int64, admission any, lastStats string) {
	t.Helper()
	if _, err := db.Exec(
		`INSERT INTO proxy_subscriptions (id, url, admission_status, last_stats) VALUES (?, 'u', ?, ?)`,
		id, admission, lastStats); err != nil {
		t.Fatal(err)
	}
}

func seedPoolNode(t *testing.T, db *sql.DB, runtimeName string, state string, subID any, config string) {
	t.Helper()
	if _, err := db.Exec(`INSERT INTO proxy_nodes
		(node_id, runtime_name, proxy_type, normalized_config, state, subscription_id, consecutive_failures)
		VALUES (?, ?, 'ss', ?, ?, ?, 0)`,
		"fp-"+runtimeName, runtimeName, config, state, subID); err != nil {
		t.Fatal(err)
	}
}

func goodConfig(name string) string {
	return `{"name": "` + name + `", "type": "ss", "server": "s.example.com", "port": 8443}`
}

func TestEligibleNodesGates(t *testing.T) {
	db, _, _ := poolTestDB(t)
	conn := mustConn(t, db)
	ctx := context.Background()
	now := time.Now()
	seedPoolSubscription(t, db, 1, AdmissionActive, "")
	seedPoolSubscription(t, db, 2, AdmissionCandidate, "")
	seedPoolSubscription(t, db, 3, nil, "")
	// 订阅 4：流量已用尽（resource_block_reason）
	seedPoolSubscription(t, db, 4, AdmissionActive, `{"traffic": "upload=90;download=10;total=100"}`)
	seedPoolSubscription(t, db, 5, AdmissionActive, "")
	seedPoolSubscription(t, db, 6, AdmissionActive, "")

	seedPoolNode(t, db, "active-1", NodeActive, int64(1), goodConfig("active-1"))
	seedPoolNode(t, db, "new-1", NodeNew, int64(1), goodConfig("new-1"))
	seedPoolNode(t, db, "stale-1", NodeStale, int64(3), goodConfig("stale-1"))
	seedPoolNode(t, db, "dead-1", NodeDead, int64(1), goodConfig("dead-1"))
	seedPoolNode(t, db, "retired-1", NodeRetired, int64(1), goodConfig("retired-1"))
	seedPoolNode(t, db, "no-sub", NodeActive, nil, goodConfig("no-sub"))
	seedPoolNode(t, db, "candidate-sub", NodeActive, int64(2), goodConfig("candidate-sub"))
	seedPoolNode(t, db, "blocked-sub", NodeActive, int64(4), goodConfig("blocked-sub"))
	// 配置不完整（缺 server）
	seedPoolNode(t, db, "incomplete", NodeActive, int64(1), `{"type": "ss", "port": 1}`)
	// 信息占位节点
	seedPoolNode(t, db, "剩余流量：10GB", NodeActive, int64(5), goodConfig("x"))
	// 订阅行不存在
	seedPoolNode(t, db, "orphan-sub", NodeActive, int64(99), goodConfig("orphan-sub"))
	// 订阅 6：admission NULL 按遗留语义仍算 ACTIVE（IsAdmitted 同口径）
	seedPoolNode(t, db, "legacy-sub", NodeActive, int64(3), goodConfig("legacy-sub"))

	nodes, err := EligibleNodes(ctx, conn, now)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, n := range nodes {
		names = append(names, n.RuntimeName)
	}
	want := []string{"active-1", "new-1", "stale-1", "legacy-sub"}
	if len(names) != len(want) {
		t.Fatalf("合格集应为 %v，实际 %v", want, names)
	}
	for i, w := range want {
		if names[i] != w {
			t.Fatalf("合格集应为 %v（按主键序），实际 %v", want, names)
		}
	}
}

func TestPoolSignatureSensitive(t *testing.T) {
	db, _, _ := poolTestDB(t)
	conn := mustConn(t, db)
	ctx := context.Background()
	now := time.Now()
	seedPoolSubscription(t, db, 1, AdmissionActive, "")
	seedPoolNode(t, db, "n1", NodeActive, int64(1), goodConfig("n1"))
	seedPoolNode(t, db, "n2", NodeActive, int64(1), goodConfig("n2"))

	sig1, err := PoolSignature(ctx, conn, t.TempDir(), now)
	if err != nil {
		t.Fatal(err)
	}
	if len(sig1) != 2 {
		t.Fatalf("两节点应两条签名，实际 %v", sig1)
	}
	// 稳定：重复计算一致
	sig1b, _ := PoolSignature(ctx, conn, t.TempDir(), now)
	if PoolSignatureChanged(sig1, sig1b) {
		t.Fatalf("重复计算签名应一致：%v vs %v", sig1, sig1b)
	}
	// 首条签名 = runtime_name:sha256(完整参数 JSON)；独立复核哈希输入
	blob := `{"name":"n1","port":8443,"server":"s.example.com","type":"ss"}`
	want := "n1:" + sha256Hex([]byte(blob))
	if sig1[0] != want {
		t.Fatalf("节点签名应 %s，实际 %s", want, sig1[0])
	}

	// 配置变化 → 签名变
	if _, err := db.Exec(`UPDATE proxy_nodes SET normalized_config = ? WHERE runtime_name = 'n1'`,
		`{"name": "n1", "type": "ss", "server": "s.example.com", "port": 8444}`); err != nil {
		t.Fatal(err)
	}
	sig2, _ := PoolSignature(ctx, conn, t.TempDir(), now)
	if !PoolSignatureChanged(sig1, sig2) {
		t.Fatalf("配置变化应改变签名")
	}

	// 数量相同但节点不同 → 签名变（比集合不比数量）
	if _, err := db.Exec(`DELETE FROM proxy_nodes WHERE runtime_name = 'n2'`); err != nil {
		t.Fatal(err)
	}
	seedPoolNode(t, db, "n3", NodeActive, int64(1), goodConfig("n3"))
	sig3, _ := PoolSignature(ctx, conn, t.TempDir(), now)
	if !PoolSignatureChanged(sig1, sig3) {
		t.Fatalf("换节点应改变签名")
	}

	// 池文件读数：文件缺席返回空
	if got := PoolFileNames(t.TempDir()); len(got) != 0 {
		t.Fatalf("无池文件应返回空，实际 %v", got)
	}
}

func TestBuildPoolWritesAndReplays(t *testing.T) {
	db, gate, dir := poolTestDB(t)
	ctx := context.Background()
	now := time.Now()
	seedPoolSubscription(t, db, 1, AdmissionActive, "")
	seedPoolNode(t, db, "n1", NodeActive, int64(1), goodConfig("n1"))
	seedPoolNode(t, db, "n2", NodeStale, int64(1), goodConfig("n2"))

	var result PoolBuildResult
	err := gate.WithWrite(ctx, store.PriorityBackground, func(c *sql.Conn) error {
		got, err := BuildPool(ctx, c, dir, now)
		if err != nil {
			return err
		}
		result = got
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.ExpectedCount != 2 || result.WrittenCount != 2 {
		t.Fatalf("数量账目不对：%+v", result)
	}
	if len(result.RuntimeNames) != 2 || result.RuntimeNames[0] != "n1" {
		t.Fatalf("运行名应按主键序：%v", result.RuntimeNames)
	}
	if names := PoolFileNames(dir); len(names) != 2 {
		t.Fatalf("池文件读数应 2 个，实际 %v", names)
	}
	if _, err := os.Stat(result.Path); err != nil {
		t.Fatalf("池文件应存在：%v", err)
	}
}

func TestRenderPoolDuplicateRejected(t *testing.T) {
	// 重名即拒绝不依赖数据库唯一约束兜底（renderPoolNodes 纯函数直测）。
	nodes := []poolNodeRow{
		{RuntimeName: "dup", Config: map[string]any{"type": "ss", "server": "s", "port": int64(1)}},
		{RuntimeName: "dup", Config: map[string]any{"type": "ss", "server": "s", "port": int64(2)}},
	}
	_, _, err := renderPoolNodes(nodes, nil)
	if err == nil {
		t.Fatal("重名应被拒绝")
	}
	if _, ok := err.(*PoolBuildError); !ok {
		t.Fatalf("应为 PoolBuildError，实际 %T", err)
	}
}

func TestBuildPoolFailurePreservesOldPool(t *testing.T) {
	db, gate, dir := poolTestDB(t)
	ctx := context.Background()
	now := time.Now()
	seedPoolSubscription(t, db, 1, AdmissionActive, "")
	// dialer-proxy 指向同订阅不存在的名字 → 渲染在写盘前失败
	seedPoolNode(t, db, "with-dp", NodeActive, int64(1),
		`{"name": "with-dp", "type": "ss", "server": "s", "port": 1, "dialer-proxy": "缺失依赖"}`)
	// 先放一个旧池，验证失败不破坏旧池
	oldText := "proxies:\n  - name: old\n    type: direct\n"
	if err := os.MkdirAll(filepath.Join(dir, "proxypool"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(PoolPath(dir), []byte(oldText), 0o644); err != nil {
		t.Fatal(err)
	}
	err := gate.WithWrite(ctx, store.PriorityBackground, func(c *sql.Conn) error {
		_, berr := BuildPool(ctx, c, dir, now)
		return berr
	})
	if err == nil {
		t.Fatal("连接依赖不完整应被拒绝")
	}
	raw, _ := os.ReadFile(PoolPath(dir))
	if string(raw) != oldText {
		t.Fatalf("构建失败不得破坏旧池，实际 %q", string(raw))
	}
}

func TestRebuildHookFiresOnSignatureChange(t *testing.T) {
	before := []string{"a:1"}
	after := []string{"a:1", "b:2"}
	fired := 0
	orig := RequestPoolRebuild
	RequestPoolRebuild = func() { fired++ }
	defer func() { RequestPoolRebuild = orig }()

	MaybeRequestPoolRebuild(before, after)
	if fired != 1 {
		t.Fatalf("签名变化应触发一次重建请求，实际 %d", fired)
	}
	MaybeRequestPoolRebuild(after, after)
	if fired != 1 {
		t.Fatalf("签名未变不得触发")
	}
}

func TestRebuildHookCountPerChange(t *testing.T) {
	orig := RequestPoolRebuild
	defer func() { RequestPoolRebuild = orig }()
	var fired atomic.Int64
	RequestPoolRebuild = func() { fired.Add(1) }
	MaybeRequestPoolRebuild([]string{"x"}, []string{"y"})
	MaybeRequestPoolRebuild([]string{"x"}, []string{"y"})
	if fired.Load() != 2 {
		t.Fatalf("两次变化应各触发一次，实际 %d", fired.Load())
	}
}

func TestSubscriptionBlockReason(t *testing.T) {
	now := time.Unix(2000000000, 0)
	cases := []struct {
		stats string
		want  string
	}{
		{"", ""},
		{"{\"traffic\": null}", ""},
		{"{\"traffic\": \"expire=0;upload=0;download=0;total=0\"}", ""},
		{"{\"traffic\": \"expire=1999999999;upload=0;download=0;total=0\"}", "订阅已到期"},
		{"{\"traffic\": \"expire=2000000001;upload=90;download=10;total=100\"}", "订阅流量已用尽"},
		{"{\"traffic\": \"expire=2000000001;upload=1;download=1;total=100\"}", ""},
		{"{\"traffic\": \"total=bad-data\"}", ""},
	}
	for _, c := range cases {
		if got := SubscriptionBlockReason(c.stats, now); got != c.want {
			t.Fatalf("SubscriptionBlockReason(%q) 应 %q，实际 %q", c.stats, c.want, got)
		}
	}
}
