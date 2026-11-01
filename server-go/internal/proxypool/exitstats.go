package proxypool

// 出口账本落库（Python exitstats.py 对齐）：作业收尾把内存里的 (出口 × 端点)
// 聚合写成 proxy_run_exits 行，并把生产结果回写节点健康账本（统一账本的第三条
// 写入通道）。fail-soft 契约：观测绝不能变成生产依赖——写不进去返回错误交调用
// 方降级，作业结果不受影响。
//
// 错误分类（与探测证据分桶，互不累加）：
//   - 环境级失败：本作业所有出口都没拿到任何 Steam 响应——不构成任何节点证据；
//   - 出口级失败：环境确认存活时，零响应出口的节点判 DEAD——DEAD 可自由复活，
//     且不累加 consecutive_failures：退休判据只归健康探测，生产抖动永远无法把
//     节点推向 RETIRED；
//   - 成功：清零失败计数、清冷却并回 ACTIVE/STALE（可复活的最强证据）。

import (
	"context"
	"database/sql"
	"time"

	"holdexar/server-go/internal/store"
)

// ExitStatsRow 一条出口聚合行（对齐 Python ExitStatsCollector.rows() 的 stats
// 形状：出口键 × 端点，请求按固定枚举分桶）。Uncertain / TunnelError 是探测
// 路径可能附加的键（Python row.get() 短路语义），收集器行缺省为零值。
type ExitStatsRow struct {
	ExitIP       string
	Endpoint     string
	Requests     int64
	Success      int64
	E429         int64
	E4XX         int64
	E5XX         int64
	Timeout      int64
	ConnectError int64
	Other        int64
	DurationMS   int64
	Uncertain    bool
	TunnelError  int64
}

// exitRowOK 该出口行拿到的 Steam 侧响应数（429/5xx 是目标响应，不罚节点）。
func exitRowOK(row ExitStatsRow) int64 {
	return row.Success + row.E429 + row.E5XX
}

const ddlProxyRunExits = `CREATE TABLE IF NOT EXISTS proxy_run_exits (
	id INTEGER NOT NULL,
	run_id INTEGER NOT NULL,
	exit_ip VARCHAR(64) NOT NULL,
	endpoint VARCHAR(32) NOT NULL,
	node VARCHAR(400),
	requests INTEGER,
	success INTEGER,
	e429 INTEGER,
	e4xx INTEGER,
	e5xx INTEGER,
	timeout INTEGER,
	connect_error INTEGER,
	other INTEGER,
	duration_ms INTEGER,
	created_at DATETIME,
	PRIMARY KEY (id)
)`

var exitSchemaIndexes = []string{
	"CREATE INDEX IF NOT EXISTS ix_pre_run_exit ON proxy_run_exits (run_id, exit_ip)",
	"CREATE INDEX IF NOT EXISTS ix_proxy_run_exits_run_id ON proxy_run_exits (run_id)",
}

// EnsureExitStatsSchema 幂等建出口账本表与索引；ctx 由写闸租约的事务传入。
func EnsureExitStatsSchema(ctx context.Context, exec interface {
	ExecContext(ctx context.Context, query string, args ...any) (sql.Result, error)
}) error {
	if _, err := exec.ExecContext(ctx, ddlProxyRunExits); err != nil {
		return err
	}
	for _, ddl := range exitSchemaIndexes {
		if _, err := exec.ExecContext(ctx, ddl); err != nil {
			return err
		}
	}
	return nil
}

// RecordRunExits 插入本次作业的出口聚合行并回写生产证据，返回插入数。
// runID=0（作业行没建起来）直接返回 0：没有作业身份的出口行无法归因。
// 事务与提交归写闸；now 为 nil 用挂钟。
func RecordRunExits(ctx context.Context, gate *store.WriteGate, runID int64,
	rows []ExitStatsRow, nodeByExit map[string]string, now *time.Time) (int, error) {
	if runID == 0 || len(rows) == 0 {
		return 0, nil
	}
	stamp := time.Now()
	if now != nil {
		stamp = *now
	}
	inserted := 0
	err := gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		inserted = 0
		if err := EnsureExitStatsSchema(ctx, conn); err != nil {
			return err
		}
		if err := writebackProductionEvidence(ctx, conn, rows, nodeByExit, stamp); err != nil {
			return err
		}
		for _, row := range rows {
			endpoint := row.Endpoint
			if endpoint == "" {
				endpoint = "other"
			}
			// 未映射到节点的出口落 NULL（Python mapping.get 的缺省形态）
			var nodeArg any
			if name := nodeByExit[row.ExitIP]; name != "" {
				nodeArg = name
			}
			if _, err := conn.ExecContext(ctx, `INSERT INTO proxy_run_exits
				(run_id, exit_ip, endpoint, node, requests, success, e429, e4xx, e5xx,
				 timeout, connect_error, other, duration_ms, created_at)
				VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
				runID, row.ExitIP, endpoint, nodeArg,
				row.Requests, row.Success, row.E429, row.E4XX, row.E5XX,
				row.Timeout, row.ConnectError, row.Other, row.DurationMS,
				pyTimeText(stamp)); err != nil {
				return err
			}
			inserted++
		}
		return nil
	})
	if err != nil {
		return 0, err
	}
	return inserted, nil
}

// writebackProductionEvidence 生产结果回写节点健康账本。
// lane 绑定提供运行名，节点指纹是另一种身份，不能拿运行名查 node_id。
func writebackProductionEvidence(ctx context.Context, conn *sql.Conn,
	rows []ExitStatsRow, nodeByExit map[string]string, stamp time.Time) error {
	type exitAgg struct{ ok, noresp int64 }
	byRuntime := map[string]exitAgg{}
	for _, row := range rows {
		runtimeName := nodeByExit[row.ExitIP]
		if runtimeName == "" {
			continue
		}
		if row.Uncertain {
			continue
		}
		if exitRowOK(row) > 0 {
			agg := byRuntime[runtimeName]
			agg.ok++
			byRuntime[runtimeName] = agg
		} else if row.Timeout+row.ConnectError+row.TunnelError > 0 {
			agg := byRuntime[runtimeName]
			agg.noresp++
			byRuntime[runtimeName] = agg
		}
	}
	if len(byRuntime) == 0 {
		return nil
	}
	// 环境存活按全部行判（含未映射到节点的出口）：任一出口拿到过 Steam 响应
	// 即本机与上游链路可用，零响应出口才构成节点证据。
	environmentAlive := false
	for _, row := range rows {
		if !row.Uncertain && exitRowOK(row) > 0 {
			environmentAlive = true
			break
		}
	}
	names := make([]string, 0, len(byRuntime))
	for name := range byRuntime {
		names = append(names, name)
	}
	nodeRows, err := loadNodeRowsByRuntime(ctx, conn, names)
	if err != nil {
		return err
	}
	// 仍归属订阅的节点才算「有来源」（归属是一对一的身份组成部分）。
	sourced := map[string]bool{}
	for _, row := range nodeRows {
		if row.SubscriptionID.Valid {
			sourced[row.NodeID] = true
		}
	}
	for _, row := range nodeRows {
		agg := byRuntime[row.RuntimeName]
		if agg.ok > 0 {
			probeOK := true
			target, err := EvaluateNodeState(row.State, EvaluateNodeStateInput{
				SourceSeen: sourced[row.NodeID], ProbeOK: &probeOK, ConsecutiveFailures: 0,
			})
			if err != nil {
				return err
			}
			if _, err := conn.ExecContext(ctx,
				`UPDATE proxy_nodes SET consecutive_failures = 0, cooldown_until = NULL,
				 state = ?, last_l2_at = ? WHERE id = ?`,
				target, pyTimeText(stamp), row.ID); err != nil {
				return err
			}
		} else if agg.noresp > 0 && environmentAlive {
			target, err := EvaluateNodeState(row.State, EvaluateNodeStateInput{
				SourceSeen:          sourced[row.NodeID],
				ProbeOK:             probeBoolPtr(false),
				ConsecutiveFailures: int(row.ConsecutiveFailures),
			})
			if err != nil {
				return err
			}
			if _, err := conn.ExecContext(ctx,
				`UPDATE proxy_nodes SET state = ?, last_l2_at = ? WHERE id = ?`,
				target, pyTimeText(stamp), row.ID); err != nil {
				return err
			}
		}
	}
	return nil
}

// LatestL0Delays 每个节点最近一次成功 L0 探测的延迟（node_id → ms）。
// 出口槽排序的延迟事实源：缺条目 = 没测过或没成功过（排序按未测处理，
// 不凭空造默认值）。延迟挂在节点行（L0 成功只更新 l0_delay_ms）。
func LatestL0Delays(ctx context.Context, db *sql.DB) (map[string]int64, error) {
	rows, err := db.QueryContext(ctx,
		`SELECT node_id, l0_delay_ms FROM proxy_nodes WHERE l0_delay_ms IS NOT NULL`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]int64{}
	for rows.Next() {
		var nodeID string
		var ms sql.NullInt64
		if err := rows.Scan(&nodeID, &ms); err != nil {
			return nil, err
		}
		if ms.Valid && ms.Int64 > 0 {
			out[nodeID] = ms.Int64
		}
	}
	return out, rows.Err()
}
