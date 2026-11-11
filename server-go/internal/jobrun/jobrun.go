package jobrun

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"holdexar/server-go/internal/store"
)

type Recorder struct {
	DB   *sql.DB
	Gate *store.WriteGate
	Now  func() time.Time
	Log  func(string, ...any)
}

func New(db *sql.DB, gate *store.WriteGate, now func() time.Time, log func(string, ...any)) *Recorder {
	return &Recorder{db, gate, now, log}
}
func encode(v any) string {
	b, e := json.Marshal(v)
	if e != nil {
		return `{"unserializable":true}`
	}
	return string(b)
}
func (r *Recorder) stamp() string { return stampAt(r.Now()) }

func stampAt(at time.Time) string {
	return at.In(time.FixedZone("Beijing", 8*3600)).Format("2006-01-02 15:04:05.000000")
}

type event struct {
	Type    string `json:"type"`
	Payload any    `json:"payload"`
}

func appendEvents(ctx context.Context, conn *sql.Conn, id, stamp string, events []event) error {
	var sequence int
	if e := conn.QueryRowContext(ctx, "SELECT COALESCE(MAX(seq),0) FROM agent_events WHERE run_id=?", id).Scan(&sequence); e != nil {
		return e
	}
	raw, e := json.Marshal(events)
	if e != nil {
		return e
	}
	_, e = conn.ExecContext(ctx, `INSERT INTO agent_events(run_id,seq,event_type,payload,created_at) SELECT ?,?+CAST(key AS INTEGER)+1,json_extract(value,'$.type'),json_extract(value,'$.payload'),? FROM json_each(?)`, id, sequence, stamp, string(raw))
	return e
}

func (r *Recorder) Run(ctx context.Context, job string, body func() (map[string]any, error)) (map[string]any, error) {
	key := make([]byte, 16)
	if _, e := rand.Read(key); e != nil {
		return nil, e
	}
	id := hex.EncodeToString(key)
	stamp := r.stamp()
	err := r.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		if _, e := conn.ExecContext(ctx, `INSERT INTO agent_sessions(id,created_at,updated_at) VALUES('runtime',?,?) ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at`, stamp, stamp); e != nil {
			return e
		}
		if _, e := conn.ExecContext(ctx, `INSERT INTO agent_runs(id,session_id,runner,"trigger",meta,status,created_at,started_at) VALUES(?,'runtime','job','scheduled',?,'running',?,?)`, id, encode(map[string]any{"job": job, "entry": "scheduled", "ref": map[string]any{}}), stamp, stamp); e != nil {
			return e
		}
		return appendEvents(ctx, conn, id, stamp, []event{{"run.state", map[string]any{"from": "queued", "to": "running"}}})
	})
	if err != nil {
		r.Log("自动任务运行记录暂未保存，继续执行 │ 任务=%s，原因=%v", job, err)
		return body()
	}
	r.Log("自动任务已开始 │ 任务=%s，运行=%s", job, id)
	result, bodyError := func() (result map[string]any, err error) {
		defer func() {
			if failure := recover(); failure != nil {
				err = fmt.Errorf("自动任务执行异常：%v", failure)
			}
		}()
		return body()
	}()
	status := "done"
	var errorCode any
	items := []event{}
	if ctx.Err() != nil && (errors.Is(bodyError, context.Canceled) || errors.Is(bodyError, context.DeadlineExceeded)) {
		status = "cancelled"
	} else if bodyError != nil {
		status = "failed"
		errorCode = "job_error"
		reason := []rune(bodyError.Error())
		if len(reason) > 200 {
			reason = reason[:200]
		}
		items = append(items, event{"task.failed", map[string]any{"reason": string(reason), "error_code": "job_error"}})
	}
	if result != nil {
		summary := map[string]any{}
		raw := encode(result)
		if len(raw) > 8192 {
			summary = map[string]any{"truncated": true, "bytes": len(raw)}
		} else {
			_ = json.Unmarshal([]byte(raw), &summary)
		}
		items = append(items, event{"step.completed", map[string]any{"step": job, "summary": summary}})
	}
	statePayload := map[string]any{"from": "running", "to": status}
	finishedPayload := map[string]any{"status": status}
	if status == "cancelled" {
		statePayload["reason"] = "runtime_stopped"
	}
	if errorCode != nil {
		finishedPayload["error_code"] = errorCode
	}
	items = append(items, event{"run.state", statePayload}, event{"run.finished", finishedPayload})
	finish, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	defer cancel()
	err = r.Gate.WithWrite(finish, store.PriorityBackground, func(conn *sql.Conn) error {
		stamp := r.stamp()
		if _, e := conn.ExecContext(finish, "UPDATE agent_runs SET status=?,error_code=?,finished_at=? WHERE id=?", status, errorCode, stamp, id); e != nil {
			return e
		}
		return appendEvents(finish, conn, id, stamp, items)
	})
	if err != nil {
		r.Log("自动任务结束记录暂未保存 │ 任务=%s，运行=%s，原因=%v", job, id, err)
	}
	r.Log("自动任务已结束 │ 任务=%s，运行=%s，状态=%s", job, id, status)
	return result, bodyError
}

func (r *Recorder) Recover(ctx context.Context) error {
	return r.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		rows, e := conn.QueryContext(ctx, `SELECT id,status FROM agent_runs WHERE runner='job' AND status IN('queued','running')`)
		if e != nil {
			return e
		}
		ids := []string{}
		previous := map[string]string{}
		for rows.Next() {
			var id, status string
			if e = rows.Scan(&id, &status); e != nil {
				rows.Close()
				return e
			}
			ids = append(ids, id)
			previous[id] = status
		}
		e = rows.Err()
		rows.Close()
		if e != nil {
			return e
		}
		if len(ids) == 0 {
			return nil
		}
		stamp := r.stamp()
		if _, e = conn.ExecContext(ctx, `UPDATE agent_runs SET status='failed',error_code='runtime_stopped',finished_at=? WHERE id IN(SELECT value FROM json_each(?))`, stamp, encode(ids)); e != nil {
			return e
		}
		values := []map[string]any{}
		for _, id := range ids {
			for seq, item := range []event{{"run.state", map[string]any{"from": previous[id], "to": "failed", "reason": "runtime_stopped"}}, {"run.finished", map[string]any{"status": "failed", "error_code": "runtime_stopped"}}} {
				values = append(values, map[string]any{"id": id, "offset": seq + 1, "type": item.Type, "payload": item.Payload})
			}
		}
		raw, e := json.Marshal(values)
		if e != nil {
			return e
		}
		_, e = conn.ExecContext(ctx, `WITH offsets AS MATERIALIZED(SELECT run_id,MAX(seq) AS last FROM agent_events GROUP BY run_id) INSERT INTO agent_events(run_id,seq,event_type,payload,created_at) SELECT json_extract(j.value,'$.id'),COALESCE(o.last,0)+json_extract(j.value,'$.offset'),json_extract(j.value,'$.type'),json_extract(j.value,'$.payload'),? FROM json_each(?) j LEFT JOIN offsets o ON o.run_id=json_extract(j.value,'$.id')`, stamp, string(raw))
		return e
	})
}
