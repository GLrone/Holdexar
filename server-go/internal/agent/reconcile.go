package agent

import (
	"context"
	"database/sql"
	"encoding/json"
	"time"

	"holdexar/server-go/internal/store"
)

// orphanStatuses 重启收尸覆盖的未终态集合：进程内执行者已随进程消失，
// 这些状态的 run 不可能再推进（Python _ORPHAN_STATUSES 同源）。
var orphanStatuses = []string{"queued", "running", "awaiting_approval"}

// stampBeijing Go 侧统一时间戳（jobrun 同款：北京时间裸时刻字符串，与
// Python DateTime 列存储格式一致，TEXT 字典序即时间序）。
func stampBeijing(at time.Time) string {
	return at.In(time.FixedZone("Beijing", 8*3600)).Format("2006-01-02 15:04:05.000000")
}

// ReconcileOrphanRuns 启动链收尸：进程重启后未终态 run 的执行者已不存在，
// 一律收敛 cancelled（reason=runtime_restarted，与用户取消区分）。只收
// created_at 早于 startedAt（进程启动锚点）的行——开门到收尸之间新建的
// 是活 run 不得误收敛。返回收敛数。
func ReconcileOrphanRuns(ctx context.Context, db *sql.DB, gate *store.WriteGate, startedAt time.Time) (int, error) {
	rows, err := db.QueryContext(ctx,
		`SELECT id,status FROM agent_runs WHERE status IN('queued','running','awaiting_approval') AND created_at < ?`,
		stampBeijing(startedAt))
	if err != nil {
		return 0, err
	}
	type orphan struct {
		id     string
		status string
	}
	var orphans []orphan
	for rows.Next() {
		var o orphan
		if err := rows.Scan(&o.id, &o.status); err != nil {
			rows.Close()
			return 0, err
		}
		orphans = append(orphans, o)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return 0, err
	}
	rows.Close()

	count := 0
	for _, o := range orphans {
		err := gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
			return convergeRun(ctx, conn, o.id, o.status, stampBeijing(time.Now()))
		})
		if err != nil {
			return count, err
		}
		count++
	}
	return count, nil
}

// convergeRun 单条收敛：状态行 + run.state/run.finished 双事件（Python
// apply_transition 终态同形；并发窗口内已被收敛的行 UPDATE 落空即跳过）。
func convergeRun(ctx context.Context, conn *sql.Conn, id, from, stamp string) error {
	result, err := conn.ExecContext(ctx,
		`UPDATE agent_runs SET status='cancelled', finished_at=? WHERE id=? AND status IN('queued','running','awaiting_approval')`,
		stamp, id)
	if err != nil {
		return err
	}
	if affected, err := result.RowsAffected(); err != nil || affected == 0 {
		return err // 已被并发收敛：跳过
	}
	var sequence int
	if err := conn.QueryRowContext(ctx, "SELECT COALESCE(MAX(seq),0) FROM agent_events WHERE run_id=?", id).Scan(&sequence); err != nil {
		return err
	}
	events := []map[string]any{
		{"type": "run.state", "payload": map[string]any{"from": from, "to": "cancelled", "reason": "runtime_restarted"}},
		{"type": "run.finished", "payload": map[string]any{"status": "cancelled", "error_code": nil}},
	}
	raw, err := json.Marshal(events)
	if err != nil {
		return err
	}
	_, err = conn.ExecContext(ctx,
		`INSERT INTO agent_events(run_id,seq,event_type,payload,created_at) SELECT ?,?+CAST(key AS INTEGER)+1,json_extract(value,'$.type'),json_extract(value,'$.payload'),? FROM json_each(?)`,
		id, sequence, stamp, string(raw))
	return err
}
