package monitoring

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	"holdexar/server-go/internal/store"
)

const StateExpression = `CASE WHEN EXISTS(SELECT 1 FROM monitor_exclusions e WHERE e.target_type=t.target_type AND e.target_id=t.target_id AND e.active=1) THEN 'excluded'
 WHEN EXISTS(SELECT 1 FROM monitor_sources s WHERE s.target_type=t.target_type AND s.target_id=t.target_id AND s.active=1) THEN 'active' ELSE 'released' END`
const priorityExpression = `COALESCE((SELECT MAX(s.priority) FROM monitor_sources s WHERE s.target_type=t.target_type AND s.target_id=t.target_id AND s.active=1),0)`

type Service struct {
	DB   *sql.DB
	Gate *store.WriteGate
	Now  func() time.Time
	Log  func(string, ...any)
}
type Change struct {
	Sources  map[int64][]string
	Managed  []string
	AddOnly  bool
	Excluded *bool
	Reason   string
}
type state struct {
	State    string
	Priority int
}
type queryer interface {
	QueryContext(context.Context, string, ...any) (*sql.Rows, error)
}

func New(db *sql.DB, gate *store.WriteGate) *Service {
	return &Service{DB: db, Gate: gate, Now: func() time.Time { return time.Now().UTC().Add(8 * time.Hour) }, Log: func(string, ...any) {}}
}
func encode(v any) string { b, _ := json.Marshal(v); return string(b) }
func dedup(ids []int64) []int64 {
	out := []int64{}
	seen := map[int64]bool{}
	for _, id := range ids {
		if !seen[id] {
			seen[id] = true
			out = append(out, id)
		}
	}
	return out
}
func sourcePriority(source string) int {
	switch source {
	case "favorite":
		return 100
	case "family_wishlist":
		return 95
	case "manual":
		return 60
	case "owned":
		return 40
	case "import":
		return 30
	case "board", "anticipated":
		return 20
	}
	return 10
}

func states(ctx context.Context, q queryer, typ string, ids []int64) (map[int64]state, error) {
	result := map[int64]state{}
	if len(ids) == 0 {
		return result, nil
	}
	rows, e := q.QueryContext(ctx, `SELECT t.target_id,`+StateExpression+`,`+priorityExpression+` FROM monitor_targets t WHERE t.target_type=? AND t.target_id IN (SELECT value FROM json_each(?))`, typ, encode(ids))
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	for rows.Next() {
		var id int64
		var s state
		if e := rows.Scan(&id, &s.State, &s.Priority); e != nil {
			return nil, e
		}
		result[id] = s
	}
	return result, rows.Err()
}

func (s *Service) States(ctx context.Context, typ string, ids []int64) (map[int64]string, error) {
	return s.statesWith(ctx, s.DB, typ, ids)
}

func (s *Service) StatesTx(ctx context.Context, conn *sql.Conn, typ string, ids []int64) (map[int64]string, error) {
	return s.statesWith(ctx, conn, typ, ids)
}
func (s *Service) SourcesTx(ctx context.Context, conn *sql.Conn, typ string, ids []int64) (map[int64][]string, error) {
	return sources(ctx, conn, typ, ids)
}
func (s *Service) statesWith(ctx context.Context, q queryer, typ string, ids []int64) (map[int64]string, error) {
	rows, e := states(ctx, q, typ, ids)
	if e != nil {
		return nil, e
	}
	out := map[int64]string{}
	for id, s := range rows {
		out[id] = s.State
	}
	return out, nil
}
func (s *Service) Sources(ctx context.Context, typ string, ids []int64) (map[int64][]string, error) {
	return sources(ctx, s.DB, typ, ids)
}
func sources(ctx context.Context, q queryer, typ string, ids []int64) (map[int64][]string, error) {
	out := map[int64][]string{}
	if len(ids) == 0 {
		return out, nil
	}
	rows, e := q.QueryContext(ctx, `SELECT target_id,source FROM monitor_sources WHERE target_type=? AND active=1 AND target_id IN(SELECT value FROM json_each(?)) ORDER BY target_id,source`, typ, encode(ids))
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	for rows.Next() {
		var id int64
		var name string
		if e := rows.Scan(&id, &name); e != nil {
			return nil, e
		}
		out[id] = append(out[id], name)
	}
	return out, rows.Err()
}

func (s *Service) Apply(ctx context.Context, typ string, ids []int64, change Change) (map[int64]string, error) {
	var result map[int64]string
	err := s.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error { var e error; result, e = s.ApplyTx(ctx, conn, typ, ids, change); return e })
	if err == nil {
		s.Log("监控资格已更新 │ 类型=%s，对象=%s", typ, encode(dedup(ids)))
	}
	return result, err
}

func (s *Service) ApplyTx(ctx context.Context, conn *sql.Conn, typ string, ids []int64, change Change) (map[int64]string, error) {
	if typ != "game" && typ != "bundle" {
		return nil, fmt.Errorf("未知 target_type: %s", typ)
	}
	ids = dedup(ids)
	result := map[int64]string{}
	if len(ids) == 0 {
		return result, nil
	}
	rawIDs := encode(ids)
	now := s.Now().Format("2006-01-02 15:04:05.000000")
	previous, e := states(ctx, conn, typ, ids)
	if e != nil {
		return nil, e
	}
	_, e = conn.ExecContext(ctx, `INSERT INTO monitor_targets(target_type,target_id,created_at,updated_at) SELECT ?,CAST(value AS INTEGER),?,? FROM json_each(?) WHERE 1 ON CONFLICT(target_type,target_id) DO NOTHING`, typ, now, now, rawIDs)
	if e != nil {
		return nil, e
	}
	if change.Excluded != nil {
		if *change.Excluded {
			reason := []rune(change.Reason)
			if len(reason) > 200 {
				reason = reason[:200]
			}
			var value any
			if len(reason) > 0 {
				value = string(reason)
			}
			_, e = conn.ExecContext(ctx, `INSERT INTO monitor_exclusions(target_type,target_id,reason,active,created_at) SELECT ?,CAST(value AS INTEGER),?,1,? FROM json_each(?) WHERE 1
 ON CONFLICT(target_type,target_id) WHERE active=1 DO UPDATE SET reason=COALESCE(excluded.reason,monitor_exclusions.reason)`, typ, value, now, rawIDs)
		} else {
			_, e = conn.ExecContext(ctx, `UPDATE monitor_exclusions SET active=0,cleared_at=? WHERE target_type=? AND active=1 AND target_id IN(SELECT value FROM json_each(?))`, now, typ, rawIDs)
		}
		if e != nil {
			return nil, e
		}
	}
	if change.Sources != nil {
		managed := map[string]bool{}
		for _, name := range change.Managed {
			managed[name] = true
		}
		wanted := [][3]any{}
		for _, id := range ids {
			seen := map[string]bool{}
			for _, name := range change.Sources[id] {
				if name == "" || seen[name] || change.Managed != nil && !managed[name] {
					continue
				}
				seen[name] = true
				wanted = append(wanted, [3]any{id, name, sourcePriority(name)})
			}
		}
		rawWanted := encode(wanted)
		if !change.AddOnly {
			query := `UPDATE monitor_sources SET active=0,updated_at=? WHERE target_type=? AND active=1 AND target_id IN(SELECT value FROM json_each(?))
 AND NOT EXISTS(SELECT 1 FROM json_each(?) d WHERE json_extract(d.value,'$[0]')=monitor_sources.target_id AND json_extract(d.value,'$[1]')=monitor_sources.source)`
			args := []any{now, typ, rawIDs, rawWanted}
			if change.Managed != nil {
				query += ` AND source IN(SELECT value FROM json_each(?))`
				args = append(args, encode(change.Managed))
			}
			if _, e = conn.ExecContext(ctx, query, args...); e != nil {
				return nil, e
			}
		}
		if len(wanted) > 0 {
			_, e = conn.ExecContext(ctx, `INSERT INTO monitor_sources(target_type,target_id,source,priority,active,created_at,updated_at)
 SELECT ?,CAST(json_extract(d.value,'$[0]') AS INTEGER),json_extract(d.value,'$[1]'),json_extract(d.value,'$[2]'),1,?,? FROM json_each(?) d WHERE 1
 ON CONFLICT(target_type,target_id,source) DO UPDATE SET active=1,priority=excluded.priority,updated_at=excluded.updated_at WHERE monitor_sources.active=0`, typ, now, now, rawWanted)
			if e != nil {
				return nil, e
			}
		}
	}
	current, e := states(ctx, conn, typ, ids)
	if e != nil {
		return nil, e
	}
	changes := [][4]any{}
	for _, id := range ids {
		after := current[id]
		before, exists := previous[id]
		if !exists {
			before = state{State: "released"}
		}
		result[id] = after.State
		if before == after {
			continue
		}
		var active, released, excluded any
		switch after.State {
		case "active":
			active = now
		case "released":
			released = now
		case "excluded":
			excluded = now
		}
		changes = append(changes, [4]any{id, active, released, excluded})
	}
	if len(changes) > 0 {
		_, e = conn.ExecContext(ctx, `UPDATE monitor_targets AS t SET updated_at=?,
 activated_at=COALESCE(json_extract(c.value,'$[1]'),t.activated_at),released_at=COALESCE(json_extract(c.value,'$[2]'),t.released_at),excluded_at=COALESCE(json_extract(c.value,'$[3]'),t.excluded_at)
 FROM json_each(?) c WHERE t.target_type=? AND t.target_id=json_extract(c.value,'$[0]')`, now, encode(changes), typ)
		if e != nil {
			return nil, e
		}
	}
	return result, nil
}

func (s *Service) Snapshot(ctx context.Context, typ string, id int64) (map[string]any, error) {
	rows, e := s.States(ctx, typ, []int64{id})
	if e != nil {
		return nil, e
	}
	sourceMap, e := s.Sources(ctx, typ, []int64{id})
	if e != nil {
		return nil, e
	}
	names := sourceMap[id]
	if names == nil {
		names = []string{}
	}
	var excluded bool
	if e = s.DB.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM monitor_exclusions WHERE target_type=? AND target_id=? AND active=1)`, typ, id).Scan(&excluded); e != nil {
		return nil, e
	}
	var value any
	if state, exists := rows[id]; exists {
		value = state
	}
	return map[string]any{"targetType": typ, "targetId": id, "state": value, "sources": names, "excluded": excluded}, nil
}

func (s *Service) Stats(ctx context.Context) (map[string]any, error) {
	catalog := map[string]int{}
	for _, typ := range []string{"game", "bundle"} {
		table := "games"
		if typ == "bundle" {
			table = "bundles"
		}
		var count int
		if e := s.DB.QueryRowContext(ctx, "SELECT count(*) FROM "+table).Scan(&count); e != nil {
			return nil, e
		}
		catalog[typ] = count
	}
	counts := map[string]int{"active": 0, "released": 0, "excluded": 0, "total": 0}
	byType := map[string]int{}
	rows, e := s.DB.QueryContext(ctx, `SELECT target_type,`+StateExpression+`,count(*) FROM monitor_targets t GROUP BY target_type,`+StateExpression)
	if e != nil {
		return nil, e
	}
	for rows.Next() {
		var typ, state string
		var n int
		if e = rows.Scan(&typ, &state, &n); e != nil {
			rows.Close()
			return nil, e
		}
		counts[state] += n
		counts["total"] += n
		byType[typ+"/"+state] = n
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return nil, e
	}
	sourceCounts := map[string]int{}
	rows, e = s.DB.QueryContext(ctx, `SELECT source,count(*) FROM monitor_sources WHERE active=1 GROUP BY source`)
	if e != nil {
		return nil, e
	}
	for rows.Next() {
		var name string
		var n int
		if e = rows.Scan(&name, &n); e != nil {
			rows.Close()
			return nil, e
		}
		sourceCounts[name] = n
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return nil, e
	}
	var exclusions int
	if e = s.DB.QueryRowContext(ctx, "SELECT count(*) FROM monitor_exclusions WHERE active=1").Scan(&exclusions); e != nil {
		return nil, e
	}
	return map[string]any{"catalog": catalog, "monitoring": counts, "byType": byType, "sources": sourceCounts, "exclusions": exclusions}, nil
}

func ValidType(typ string) bool { return typ == "game" || typ == "bundle" }
