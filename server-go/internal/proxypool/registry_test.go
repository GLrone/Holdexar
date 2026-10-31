package proxypool

import (
	"context"
	"database/sql"
	"fmt"
	"path/filepath"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
)

// newTestDB 隔离临时库 + 写闸 + 两表 schema + proxy_subscriptions 夹具。
func newTestDB(t *testing.T) (*sql.DB, *store.WriteGate, string) {
	t.Helper()
	dir := t.TempDir()
	db, err := store.Open(filepath.Join(dir, "test.db"))
	if err != nil {
		t.Fatalf("打开测试库失败：%v", err)
	}
	t.Cleanup(func() { db.Close() })
	gate := store.NewWriteGate(db, filepath.Join(dir, "test.db"))
	if _, err := db.Exec(`CREATE TABLE proxy_subscriptions (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		url TEXT NOT NULL,
		last_fetch_at DATETIME, last_fetch_status VARCHAR(32),
		last_success_at DATETIME, last_error VARCHAR(500), cache_url_identity VARCHAR(64))`); err != nil {
		t.Fatal(err)
	}
	return db, gate, dir
}

// sampleNodes 两节点订阅内容（与 sampleSubscription 同构的最小形态）。
func sampleNodes() []map[string]any {
	return []map[string]any{
		{"name": "香港 01", "type": "ss", "server": "hk1.example.com", "port": int64(8443),
			"cipher": "aes-256-gcm", "password": "p1", "udp": true},
		{"name": "美国 02", "type": "vmess", "server": "us2.example.com", "port": int64(443),
			"uuid": "aaaa-bbbb", "tls": true},
	}
}

func snapshotOf(subID int64, url string, nodes []map[string]any, now time.Time) *Snapshot {
	raw := []byte("raw-" + url)
	return &Snapshot{
		SubscriptionID: subID, URL: url, FetchedAt: now, HTTPStatus: 200,
		ContentType: "text/yaml", Sha256: sha256Hex(raw), RawContent: raw,
		Fmt: FormatYAML, NodeCount: len(nodes), SourceChannel: ChannelDirect,
		Nodes: nodes,
	}
}

func mustApply(t *testing.T, conn *sql.Conn, subID int64, snap *Snapshot, now time.Time, tag string) ApplyResult {
	t.Helper()
	res, err := ApplySnapshot(context.Background(), conn, subID, snap, now, tag)
	if err != nil {
		t.Fatalf("ApplySnapshot 失败：%v", err)
	}
	return res
}

func TestApplySnapshotCreatesNodes(t *testing.T) {
	_, gate, _ := newTestDB(t)
	ctx := context.Background()
	now := time.Date(2026, 10, 31, 12, 0, 0, 0, time.Local)
	err := gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		if err := EnsureSchema(ctx, conn); err != nil {
			return err
		}
		nodes := sampleNodes()
		snap := snapshotOf(1, "https://sub.example.com/a", nodes, now)
		// 快照行先行（顺序语义判据依赖落库 id）
		if _, err := PersistSnapshot(ctx, conn, t.TempDir(), snap); err != nil {
			return err
		}
		res := mustApply(t, conn, 1, snap, now, SubscriptionTag("https://sub.example.com/a"))
		if len(res.CreatedNodes) != 2 || len(res.RefreshedNodes) != 0 {
			t.Fatalf("首轮应新建 2 节点：%+v", res)
		}
		for _, fp := range res.CreatedNodes {
			var state, runtimeName, originalName string
			var subID sql.NullInt64
			if err := conn.QueryRowContext(ctx,
				`SELECT state, runtime_name, original_name, subscription_id FROM proxy_nodes WHERE node_id = ?`,
				fp).Scan(&state, &runtimeName, &originalName, &subID); err != nil {
				t.Fatal(err)
			}
			if state != NodeNew {
				// 登记时 state=NEW；来源在场不动状态（只推进 last_source_seen），
				// NEW→ACTIVE/STALE 由体检链用真实探测结果决定
				t.Fatalf("登记时来源在场且无体检证据 → 保持 NEW，got %s", state)
			}
			_ = runtimeName
			_ = originalName
			if !subID.Valid || subID.Int64 != 1 {
				t.Fatalf("归属订阅应为 1：%v", subID)
			}
		}
		// runtime_name 铸造：<订阅>|原名
		var rn string
		if err := conn.QueryRowContext(ctx,
			`SELECT runtime_name FROM proxy_nodes WHERE original_name = '香港 01'`).Scan(&rn); err != nil {
			t.Fatal(err)
		}
		if rn != "1|香港 01" {
			t.Fatalf("runtime_name 应为 1|香港 01，got %q", rn)
		}
		return nil
	})
	if err != nil {
		t.Fatalf("写闸事务失败：%v", err)
	}
}

func TestApplySnapshotReplayAndRefresh(t *testing.T) {
	_, gate, _ := newTestDB(t)
	ctx := context.Background()
	now := time.Date(2026, 10, 31, 12, 0, 0, 0, time.Local)
	tag := SubscriptionTag("https://sub.example.com/a")
	err := gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		if err := EnsureSchema(ctx, conn); err != nil {
			return err
		}
		apply := func(nodes []map[string]any) ApplyResult {
			snap := snapshotOf(1, "https://sub.example.com/a", nodes, now)
			if _, err := PersistSnapshot(ctx, conn, t.TempDir(), snap); err != nil {
				t.Fatal(err)
			}
			return mustApply(t, conn, 1, snap, now, tag)
		}
		first := apply(sampleNodes())
		if len(first.CreatedNodes) != 2 {
			t.Fatalf("首轮 created=2：%+v", first)
		}
		// 同内容重放：跳过同值重写
		replay := apply(sampleNodes())
		if !replay.SkippedIdentical {
			t.Fatalf("同内容重放应 skipped_identical：%+v", replay)
		}
		// 内容变更：一节点换凭据（新指纹=新建），另一节点流失（解除归属）
		changed := []map[string]any{
			{"name": "香港 01", "type": "ss", "server": "hk1.example.com", "port": int64(8443),
				"cipher": "aes-256-gcm", "password": "rotated", "udp": true},
		}
		second := apply(changed)
		if len(second.CreatedNodes) != 1 {
			t.Fatalf("换凭据应新建 1：%+v", second)
		}
		if len(second.RemovedSources) != 2 {
			t.Fatalf("快照只含 1 节点，旧 2 来源应全部解除归属：%+v", second.RemovedSources)
		}
		// 流失节点行保留（身份台账不删），归属挂空
		var count int
		if err := conn.QueryRowContext(ctx, `SELECT COUNT(*) FROM proxy_nodes`).Scan(&count); err != nil {
			t.Fatal(err)
		}
		if count != 3 {
			t.Fatalf("行数应为 3（2 旧行保留 + 1 新建），got %d", count)
		}
		var orphaned int
		if err := conn.QueryRowContext(ctx,
			`SELECT COUNT(*) FROM proxy_nodes WHERE subscription_id IS NULL`).Scan(&orphaned); err != nil {
			t.Fatal(err)
		}
		if orphaned != 2 {
			t.Fatalf("流失节点应解除归属，got %d", orphaned)
		}
		return nil
	})
	if err != nil {
		t.Fatalf("写闸事务失败：%v", err)
	}
}

func TestApplySnapshotStaleGuard(t *testing.T) {
	_, gate, _ := newTestDB(t)
	ctx := context.Background()
	now := time.Date(2026, 10, 31, 12, 0, 0, 0, time.Local)
	tag := SubscriptionTag("https://sub.example.com/a")
	err := gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		if err := EnsureSchema(ctx, conn); err != nil {
			return err
		}
		apply := func(url string, nodes []map[string]any) ApplyResult {
			snap := snapshotOf(1, url, nodes, now)
			if _, err := PersistSnapshot(ctx, conn, t.TempDir(), snap); err != nil {
				t.Fatal(err)
			}
			return mustApply(t, conn, 1, snap, now, tag)
		}
		urlA := "https://sub.example.com/a"
		urlB := "https://sub.example.com/b"
		apply(urlA, sampleNodes())
		// 新内容成功落库（B 成为最新成功快照）
		apply(urlB, sampleNodes()[:1])
		// 旧内容 A 的输入再来：其落库 id 旧于 B → 整体跳过
		staleSnap := snapshotOf(1, urlA, sampleNodes(), now)
		stale := mustApply(t, conn, 1, staleSnap, now, tag)
		if !stale.SkippedStale {
			t.Fatalf("旧快照输入应 skipped_stale：%+v", stale)
		}
		return nil
	})
	if err != nil {
		t.Fatalf("写闸事务失败：%v", err)
	}
}

func TestSettleNodesAfterSourceChangeDeadRevival(t *testing.T) {
	_, gate, _ := newTestDB(t)
	ctx := context.Background()
	now := time.Date(2026, 10, 31, 12, 0, 0, 0, time.Local)
	tag := SubscriptionTag("https://sub.example.com/a")
	err := gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		if err := EnsureSchema(ctx, conn); err != nil {
			return err
		}
		apply := func(nodes []map[string]any) error {
			// 每轮 raw 不同（订阅刷新产出新字节），节点集可同可异
			url := fmt.Sprintf("https://sub.example.com/round%d", timeRoundAdd())
			snap := snapshotOf(1, url, nodes, now)
			if _, err := PersistSnapshot(ctx, conn, t.TempDir(), snap); err != nil {
				return err
			}
			_, err := ApplySnapshot(ctx, conn, 1, snap, now, tag)
			return err
		}
		// 轮 1：提供节点
		if err := apply(sampleNodes()[:1]); err != nil {
			return err
		}
		// 轮 2：订阅撤掉它 → 解除归属；体检失败链把它打成 DEAD
		if err := apply(nil); err != nil {
			return err
		}
		if _, err := conn.ExecContext(ctx,
			`UPDATE proxy_nodes SET state = 'DEAD', consecutive_failures = 3`); err != nil {
			return err
		}
		// 轮 3：同一配置重新被提供（归属解除行不在本订阅名下 → 不判同值重放）
		if err := apply(sampleNodes()[:1]); err != nil {
			return err
		}
		var state string
		if err := conn.QueryRowContext(ctx,
			`SELECT state FROM proxy_nodes WHERE original_name = '香港 01'`).Scan(&state); err != nil {
			return err
		}
		if state != NodeStale {
			t.Fatalf("订阅重新提供的 DEAD 节点应回 STALE 重参选，got %s", state)
		}
		var failures int64
		if err := conn.QueryRowContext(ctx,
			`SELECT consecutive_failures FROM proxy_nodes WHERE original_name = '香港 01'`).Scan(&failures); err != nil {
			return err
		}
		if failures != 0 {
			t.Fatalf("放回重测应清零失败计数，got %d", failures)
		}
		return nil
	})
	if err != nil {
		t.Fatalf("写闸事务失败：%v", err)
	}
}

var testRound int64

func timeRoundAdd() int64 {
	testRound++
	return testRound
}

func TestRuntimeNameCollisionAcrossRefresh(t *testing.T) {
	_, gate, _ := newTestDB(t)
	ctx := context.Background()
	now := time.Date(2026, 10, 31, 12, 0, 0, 0, time.Local)
	tag := SubscriptionTag("https://sub.example.com/a")
	err := gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		if err := EnsureSchema(ctx, conn); err != nil {
			return err
		}
		apply := func(nodes []map[string]any) ApplyResult {
			snap := snapshotOf(1, "https://sub.example.com/a", nodes, now)
			if _, err := PersistSnapshot(ctx, conn, t.TempDir(), snap); err != nil {
				t.Fatal(err)
			}
			return mustApply(t, conn, 1, snap, now, tag)
		}
		// 同名不同凭据 = 两个节点，runtime_name 撞名加 #2
		nodes := []map[string]any{
			{"name": "同名", "type": "ss", "server": "a.example.com", "port": int64(1), "password": "v1"},
			{"name": "同名", "type": "ss", "server": "b.example.com", "port": int64(2), "password": "v2"},
		}
		res := apply(nodes)
		if len(res.CreatedNodes) != 2 {
			t.Fatalf("应新建 2：%+v", res)
		}
		var names []string
		rows, err := conn.QueryContext(ctx, `SELECT runtime_name FROM proxy_nodes ORDER BY runtime_name`)
		if err != nil {
			return err
		}
		for rows.Next() {
			var n string
			if err := rows.Scan(&n); err != nil {
				return err
			}
			names = append(names, n)
		}
		rows.Close()
		if len(names) != 2 || names[0] != "1|同名" || names[1] != "1|同名#2" {
			t.Fatalf("撞名应铸造 #2：%v", names)
		}
		return nil
	})
	if err != nil {
		t.Fatalf("写闸事务失败：%v", err)
	}
}

func TestExitIPInheritance(t *testing.T) {
	_, gate, _ := newTestDB(t)
	ctx := context.Background()
	now := time.Date(2026, 10, 31, 12, 0, 0, 0, time.Local)
	tag := SubscriptionTag("https://sub.example.com/a")
	err := gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		if err := EnsureSchema(ctx, conn); err != nil {
			return err
		}
		round := 0
		apply := func(nodes []map[string]any) ApplyResult {
			round++
			// 每轮拉取的字节不同（sha 不同），节点集可变
			snap := snapshotOf(1, fmt.Sprintf("https://sub.example.com/round%d", round), nodes, now)
			if _, err := PersistSnapshot(ctx, conn, t.TempDir(), snap); err != nil {
				t.Fatal(err)
			}
			return mustApply(t, conn, 1, snap, now, tag)
		}
		// 首轮：节点带已知出口 IP（体检结论的持久化形态）
		v1 := []map[string]any{
			{"name": "机场-东京", "type": "vmess", "server": "jp1.example.com", "port": int64(443), "uuid": "u1"},
		}
		if res := apply(v1); len(res.CreatedNodes) != 1 {
			t.Fatalf("首轮应建 1：%+v", res)
		}
		if _, err := conn.ExecContext(ctx,
			`UPDATE proxy_nodes SET exit_ip = '203.0.113.7'`); err != nil {
			return err
		}
		// 机场换内容版本号：名字加 #9（指纹变、名字主干与端点不变）
		v2 := []map[string]any{
			{"name": "机场-东京 #9", "type": "vmess", "server": "jp1.example.com", "port": int64(443), "uuid": "u2"},
		}
		res := apply(v2)
		if len(res.CreatedNodes) != 1 || len(res.RemovedSources) != 1 {
			t.Fatalf("版本号更替应 1 建 1 撤：%+v", res)
		}
		var exitIP sql.NullString
		if err := conn.QueryRowContext(ctx,
			`SELECT exit_ip FROM proxy_nodes WHERE original_name = '机场-东京 #9'`).Scan(&exitIP); err != nil {
			return err
		}
		if !exitIP.Valid || exitIP.String != "203.0.113.7" {
			t.Fatalf("新化身应继承旧化身出口身份，got %v", exitIP)
		}
		return nil
	})
	if err != nil {
		t.Fatalf("写闸事务失败：%v", err)
	}
}
