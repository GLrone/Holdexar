package agent

import (
	"context"
	"database/sql"
	"encoding/json"
	"time"

	"holdexar/server-go/internal/store"
	"holdexar/server-go/internal/timeutil"
)

var orphanStatuses = []string{"queued", "running", "awaiting_approval"}

func stampBeijing(at time.Time) string {
	return at.In(timeutil.ZoneBeijing).Format("2006-01-02 15:04:05.000000")
}

// ReconcileOrphanRuns 启动时仅收敛早于 startedAt 的未终态 run 为 cancelled（runtime_restarted），防误杀新 run。
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

func convergeRun(ctx context.Context, conn *sql.Conn, id, from, stamp string) error {
	result, err := conn.ExecContext(ctx,
		`UPDATE agent_runs SET status='cancelled', finished_at=? WHERE id=? AND status IN('queued','running','awaiting_approval')`,
		stamp, id)
	if err != nil {
		return err
	}
	if affected, err := result.RowsAffected(); err != nil || affected == 0 {
		return err
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
