package agent

import (
	"context"
	"database/sql"
	"encoding/json"
	"path/filepath"
	"reflect"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
)

const reconcileSchema = `
CREATE TABLE agent_sessions (
	id VARCHAR(32) NOT NULL, created_at DATETIME, updated_at DATETIME, PRIMARY KEY (id)
);
CREATE TABLE agent_runs (
	id VARCHAR(32) NOT NULL,
	session_id VARCHAR(32) NOT NULL,
	runner VARCHAR(16) NOT NULL,
	"trigger" VARCHAR(16) NOT NULL,
	meta JSON,
	status VARCHAR(24) NOT NULL,
	error_code VARCHAR(64),
	created_at DATETIME,
	started_at DATETIME,
	finished_at DATETIME,
	PRIMARY KEY (id),
	FOREIGN KEY(session_id) REFERENCES agent_sessions (id) ON DELETE CASCADE
);
CREATE TABLE agent_events (
	id INTEGER NOT NULL,
	run_id VARCHAR(32) NOT NULL,
	seq INTEGER NOT NULL,
	event_type VARCHAR(32) NOT NULL,
	payload JSON,
	created_at DATETIME,
	PRIMARY KEY (id),
	CONSTRAINT ux_agent_event_run_seq UNIQUE (run_id, seq),
	FOREIGN KEY(run_id) REFERENCES agent_runs (id) ON DELETE CASCADE
);
`

func newReconcileDB(t *testing.T) (*sql.DB, *store.WriteGate) {
	t.Helper()
	db, err := store.Open(filepath.Join(t.TempDir(), "holdexar.db"))
	if err != nil {
		t.Fatalf("打开测试库失败: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	if _, err := db.Exec(reconcileSchema); err != nil {
		t.Fatalf("建表失败: %v", err)
	}
	if _, err := db.Exec(
		`INSERT INTO agent_sessions(id,created_at,updated_at) VALUES('sess','2026-01-01 08:00:00.000000','2026-01-01 08:00:00.000000')`); err != nil {
		t.Fatalf("会话种子失败: %v", err)
	}
	return db, store.NewWriteGate(db, filepath.Join(t.TempDir(), "gate.lock"))
}

func seedRun(t *testing.T, db *sql.DB, id, status string, createdBefore bool) {
	t.Helper()
	stamp := "2026-01-01 08:00:00.000000"
	if !createdBefore {
		stamp = "2099-01-01 08:00:00.000000"
	}
	if _, err := db.Exec(
		`INSERT INTO agent_runs(id,session_id,runner,"trigger",meta,status,created_at,started_at) VALUES(?, 'sess','agent','manual','{}',?,?,?)`,
		id, status, stamp, stamp); err != nil {
		t.Fatalf("run 种子失败: %v", err)
	}
}

func runStatus(t *testing.T, db *sql.DB, id string) string {
	t.Helper()
	var status sql.NullString
	if err := db.QueryRow(`SELECT status FROM agent_runs WHERE id=?`, id).Scan(&status); err != nil {
		t.Fatalf("读取 run 状态失败: %v", err)
	}
	return status.String
}

func TestReconcileOrphans(t *testing.T) {
	db, gate := newReconcileDB(t)
	seedRun(t, db, "ridq", "queued", true)
	seedRun(t, db, "ridr", "running", true)
	seedRun(t, db, "rida", "awaiting_approval", true)
	seedRun(t, db, "ridd", "done", true)

	count, err := ReconcileOrphanRuns(context.Background(), db, gate, time.Now())
	if err != nil {
		t.Fatalf("收尸失败: %v", err)
	}
	if count != 3 {
		t.Fatalf("应收敛 3 条，实际 %d", count)
	}
	for id := range map[string]string{"ridq": "", "ridr": "", "rida": ""} {
		if got := runStatus(t, db, id); got != "cancelled" {
			t.Fatalf("%s 应为 cancelled，实际 %s", id, got)
		}
	}
	if got := runStatus(t, db, "ridd"); got != "done" {
		t.Fatalf("终态 run 不得被收尸，实际 %s", got)
	}
	var payload string
	if err := db.QueryRow(
		`SELECT payload FROM agent_events WHERE run_id='ridq' AND event_type='run.state'`).Scan(&payload); err != nil {
		t.Fatalf("读取 run.state 事件失败: %v", err)
	}
	var state map[string]any
	if err := json.Unmarshal([]byte(payload), &state); err != nil {
		t.Fatalf("run.state 载荷解析失败: %v", err)
	}
	want := map[string]any{"from": "queued", "to": "cancelled", "reason": "runtime_restarted"}
	if !reflect.DeepEqual(state, want) {
		t.Fatalf("run.state 载荷不符：got %v want %v", state, want)
	}
	var finished string
	if err := db.QueryRow(
		`SELECT payload FROM agent_events WHERE run_id='ridq' AND event_type='run.finished'`).Scan(&finished); err != nil {
		t.Fatalf("读取 run.finished 事件失败: %v", err)
	}
	var fin map[string]any
	if err := json.Unmarshal([]byte(finished), &fin); err != nil {
		t.Fatalf("run.finished 载荷解析失败: %v", err)
	}
	if !reflect.DeepEqual(fin, map[string]any{"status": "cancelled", "error_code": nil}) {
		t.Fatalf("run.finished 载荷不符：%v", fin)
	}
}

func TestReconcileEmpty(t *testing.T) {
	db, gate := newReconcileDB(t)
	count, err := ReconcileOrphanRuns(context.Background(), db, gate, time.Now())
	if err != nil {
		t.Fatalf("收尸失败: %v", err)
	}
	if count != 0 {
		t.Fatalf("空账本应收敛 0 条，实际 %d", count)
	}
}

func TestReconcileSparesRunsCreatedAfterStart(t *testing.T) {
	db, gate := newReconcileDB(t)
	seedRun(t, db, "live", "queued", false)
	count, err := ReconcileOrphanRuns(context.Background(), db, gate, time.Now())
	if err != nil {
		t.Fatalf("收尸失败: %v", err)
	}
	if count != 0 {
		t.Fatalf("锚点后的 run 不得收敛，实际收敛 %d", count)
	}
	if got := runStatus(t, db, "live"); got != "queued" {
		t.Fatalf("活 run 应保持 queued，实际 %s", got)
	}
}
