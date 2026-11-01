package proxypool

// 出口账本与生产证据回写测试：出口成功清冷却复活、环境级失败不罚、
// 出口级失败判死不累加、账本行落库形状、L0 延迟读点。

import (
	"context"
	"database/sql"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
)

func exitTestDB(t *testing.T) (*sql.DB, *store.WriteGate, string) {
	t.Helper()
	db, gate, dir := newTestDB(t)
	if err := EnsureSchema(context.Background(), mustConn(t, db)); err != nil {
		t.Fatal(err)
	}
	if err := EnsureExitStatsSchema(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	return db, gate, dir
}

func TestRecordRunExitsSkipsWithoutRunID(t *testing.T) {
	db, gate, _ := exitTestDB(t)
	ctx := context.Background()
	n, err := RecordRunExits(ctx, gate, 0, []ExitStatsRow{{ExitIP: "1.2.3.4"}}, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if n != 0 {
		t.Fatalf("无作业身份应返回 0，实际 %d", n)
	}
	n, err = RecordRunExits(ctx, gate, 5, nil, nil, nil)
	if err != nil || n != 0 {
		t.Fatalf("无行应返回 0：%d %v", n, err)
	}
	if got := queryInt(t, db, `SELECT COUNT(*) FROM proxy_run_exits`); got != 0 {
		t.Fatalf("不应落行，实际 %d", got)
	}
}

func TestRecordRunExitsInsertsRows(t *testing.T) {
	db, gate, _ := exitTestDB(t)
	ctx := context.Background()
	now := time.Now()
	rows := []ExitStatsRow{
		{ExitIP: "1.1.1.1", Endpoint: "store_browse", Requests: 10, Success: 7,
			E429: 1, E4XX: 1, E5XX: 0, Timeout: 1, ConnectError: 0, Other: 0, DurationMS: 5000},
		{ExitIP: "2.2.2.2", Endpoint: "", Requests: 3, Other: 3, DurationMS: 900},
	}
	n, err := RecordRunExits(ctx, gate, 42, rows,
		map[string]string{"1.1.1.1": "node-a"}, &now)
	if err != nil {
		t.Fatal(err)
	}
	if n != 2 {
		t.Fatalf("应插 2 行，实际 %d", n)
	}
	var endpoint, node sql.NullString
	var requests, success, e429 int64
	if err := db.QueryRow(`SELECT endpoint, node, requests, success, e429 FROM proxy_run_exits
		WHERE run_id = 42 AND exit_ip = '1.1.1.1'`).
		Scan(&endpoint, &node, &requests, &success, &e429); err != nil {
		t.Fatal(err)
	}
	if endpoint.String != "store_browse" || node.String != "node-a" ||
		requests != 10 || success != 7 || e429 != 1 {
		t.Fatalf("聚合行形状不对：endpoint=%q node=%q requests=%d success=%d e429=%d",
			endpoint.String, node.String, requests, success, e429)
	}
	// 端点缺省 other；node 缺席为 NULL
	if err := db.QueryRow(`SELECT endpoint, node FROM proxy_run_exits
		WHERE run_id = 42 AND exit_ip = '2.2.2.2'`).Scan(&endpoint, &node); err != nil {
		t.Fatal(err)
	}
	if endpoint.String != "other" || node.Valid {
		t.Fatalf("缺省端点/缺席节点不对：%+v %+v", endpoint, node)
	}
}

func TestExitSuccessClearsCooldownAndRevives(t *testing.T) {
	db, gate, _ := exitTestDB(t)
	ctx := context.Background()
	now := time.Now()
	// DEAD 且带失败计数与冷却的节点，出口拿到 Steam 响应 → 复活 ACTIVE 清冷却
	seedNode(t, db, "node-a", NodeDead, 3, int64(1))
	if _, err := db.Exec(`UPDATE proxy_nodes SET cooldown_until = ?, exit_ip = '1.1.1.1' WHERE runtime_name = 'node-a'`,
		pyTimeText(now.Add(time.Hour))); err != nil {
		t.Fatal(err)
	}
	rows := []ExitStatsRow{
		{ExitIP: "1.1.1.1", Endpoint: "store_browse", Requests: 4, Success: 3, Timeout: 1},
	}
	n, err := RecordRunExits(ctx, gate, 1, rows, map[string]string{"1.1.1.1": "node-a"}, &now)
	if err != nil || n != 1 {
		t.Fatalf("落行失败：%d %v", n, err)
	}
	var state string
	var failures int64
	var cooldown sql.NullString
	if err := db.QueryRow(`SELECT state, consecutive_failures, CAST(cooldown_until AS TEXT)
		FROM proxy_nodes WHERE runtime_name = 'node-a'`).Scan(&state, &failures, &cooldown); err != nil {
		t.Fatal(err)
	}
	if state != NodeActive || failures != 0 || cooldown.Valid {
		t.Fatalf("成功出口应复活清冷却：state=%s failures=%d cooldown=%v", state, failures, cooldown)
	}
}

func TestExitFailureDeadWithoutCounting(t *testing.T) {
	db, gate, _ := exitTestDB(t)
	ctx := context.Background()
	now := time.Now()
	// 环境存活（另一出口有响应），本出口零响应 → DEAD，但失败计数不累加
	seedNode(t, db, "node-a", NodeStale, 1, int64(1))
	seedNode(t, db, "node-b", NodeActive, 0, int64(1))
	rows := []ExitStatsRow{
		{ExitIP: "1.1.1.1", Endpoint: "store_browse", Requests: 5, Timeout: 5},
		{ExitIP: "2.2.2.2", Endpoint: "store_browse", Requests: 5, Success: 5},
	}
	if _, err := RecordRunExits(ctx, gate, 1, rows,
		map[string]string{"1.1.1.1": "node-a", "2.2.2.2": "node-b"}, &now); err != nil {
		t.Fatal(err)
	}
	var state string
	var failures int64
	if err := db.QueryRow(`SELECT state, consecutive_failures FROM proxy_nodes WHERE runtime_name = 'node-a'`).
		Scan(&state, &failures); err != nil {
		t.Fatal(err)
	}
	if state != NodeDead || failures != 1 {
		t.Fatalf("零响应出口应判 DEAD 且不累加计数：state=%s failures=%d", state, failures)
	}
	// 成功出口保持 ACTIVE 清零
	var stateB string
	var failuresB int64
	if err := db.QueryRow(`SELECT state, consecutive_failures FROM proxy_nodes WHERE runtime_name = 'node-b'`).
		Scan(&stateB, &failuresB); err != nil {
		t.Fatal(err)
	}
	if stateB != NodeActive || failuresB != 0 {
		t.Fatalf("成功出口应保持 ACTIVE：%s %d", stateB, failuresB)
	}
}

func TestEnvironmentDownNoPenalty(t *testing.T) {
	db, gate, _ := exitTestDB(t)
	ctx := context.Background()
	now := time.Now()
	// 本作业所有出口都没拿到任何 Steam 响应：不构成节点证据
	seedNode(t, db, "node-a", NodeActive, 0, int64(1))
	rows := []ExitStatsRow{
		{ExitIP: "1.1.1.1", Endpoint: "store_browse", Requests: 5, Timeout: 5},
	}
	if _, err := RecordRunExits(ctx, gate, 1, rows,
		map[string]string{"1.1.1.1": "node-a"}, &now); err != nil {
		t.Fatal(err)
	}
	var state string
	if err := db.QueryRow(`SELECT state FROM proxy_nodes WHERE runtime_name = 'node-a'`).
		Scan(&state); err != nil {
		t.Fatal(err)
	}
	if state != NodeActive {
		t.Fatalf("环境级失败不得罚节点：state=%s", state)
	}
}

func TestUncertainRowsIgnored(t *testing.T) {
	db, gate, _ := exitTestDB(t)
	ctx := context.Background()
	now := time.Now()
	seedNode(t, db, "node-a", NodeActive, 2, int64(1))
	// uncertain 行跳过聚合；另一出口成功 → 环境存活但 node-a 无 noresp（行被跳过）
	rows := []ExitStatsRow{
		{ExitIP: "1.1.1.1", Endpoint: "store_browse", Requests: 5, Timeout: 5, Uncertain: true},
		{ExitIP: "2.2.2.2", Endpoint: "store_browse", Requests: 2, Success: 2},
	}
	if _, err := RecordRunExits(ctx, gate, 1, rows,
		map[string]string{"1.1.1.1": "node-a", "2.2.2.2": "node-b-missing"}, &now); err != nil {
		t.Fatal(err)
	}
	var state string
	var failures int64
	if err := db.QueryRow(`SELECT state, consecutive_failures FROM proxy_nodes WHERE runtime_name = 'node-a'`).
		Scan(&state, &failures); err != nil {
		t.Fatal(err)
	}
	if state != NodeActive || failures != 2 {
		t.Fatalf("uncertain 行不得构成节点证据：state=%s failures=%d", state, failures)
	}
}

func TestTunnelErrorCountsAsNoResponse(t *testing.T) {
	db, gate, _ := exitTestDB(t)
	ctx := context.Background()
	now := time.Now()
	seedNode(t, db, "node-a", NodeStale, 0, int64(1))
	seedNode(t, db, "node-b", NodeActive, 0, int64(1))
	rows := []ExitStatsRow{
		{ExitIP: "1.1.1.1", Endpoint: "store_browse", Requests: 2, TunnelError: 2},
		{ExitIP: "2.2.2.2", Endpoint: "store_browse", Requests: 2, Success: 2},
	}
	if _, err := RecordRunExits(ctx, gate, 1, rows,
		map[string]string{"1.1.1.1": "node-a", "2.2.2.2": "node-b"}, &now); err != nil {
		t.Fatal(err)
	}
	var state string
	if err := db.QueryRow(`SELECT state FROM proxy_nodes WHERE runtime_name = 'node-a'`).
		Scan(&state); err != nil {
		t.Fatal(err)
	}
	if state != NodeDead {
		t.Fatalf("tunnel_error 应计零响应：state=%s", state)
	}
}

func TestLatestL0DelaysReadPoint(t *testing.T) {
	db, _, _ := poolTestDB(t)
	seedNode(t, db, "a", NodeActive, 0, int64(1))
	seedNode(t, db, "b", NodeActive, 0, int64(1))
	seedNode(t, db, "c", NodeDead, 0, int64(1))
	if _, err := db.Exec(`UPDATE proxy_nodes SET l0_delay_ms = 120 WHERE runtime_name = 'a'`); err != nil {
		t.Fatal(err)
	}
	// 未成功过的（NULL）与非法值（0 / 负数）都不进读点
	if _, err := db.Exec(`UPDATE proxy_nodes SET l0_delay_ms = 0 WHERE runtime_name = 'b'`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`UPDATE proxy_nodes SET l0_delay_ms = -5 WHERE runtime_name = 'c'`); err != nil {
		t.Fatal(err)
	}
	delays, err := LatestL0Delays(context.Background(), db)
	if err != nil {
		t.Fatal(err)
	}
	if len(delays) != 1 || delays["fp-a"] != 120 {
		t.Fatalf("延迟读点应只含成功正延迟： %+v", delays)
	}
}
