package proxypool

// 错误分桶（互不累加）：全出口零响应视为环境失败不记证据；环境存活时零响应仅判 DEAD，不累加 consecutive_failures。
// 生产成功清零失败计数与冷却并恢复 ACTIVE/STALE。

import (
	"context"
	"database/sql"
	"time"

	"holdexar/server-go/internal/store"
)

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
