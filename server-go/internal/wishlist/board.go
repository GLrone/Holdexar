package wishlist

import (
	"context"
	"database/sql"
	"strings"
	"time"

	"holdexar/server-go/internal/monitoring"
	"holdexar/server-go/internal/store"
)

func (p *Pool) EnsureBoard(ctx context.Context, input []int64, source string) (map[string]int, error) {
	ids := cleanPoolIDs(input)
	result := map[string]int{"added": 0, "exists": 0, "skipped": 0}
	if len(ids) == 0 {
		return result, nil
	}
	if strings.TrimSpace(source) == "" {
		source = "board"
	}
	err := p.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		states, e := p.Monitor.StatesTx(ctx, conn, "game", ids)
		if e != nil {
			return e
		}
		rows, e := conn.QueryContext(ctx, `SELECT j.value,s.active,EXISTS(SELECT 1 FROM monitor_exclusions x WHERE x.target_type='game' AND x.target_id=j.value AND x.active=1) FROM json_each(?) j LEFT JOIN monitor_sources s ON s.target_type='game' AND s.target_id=j.value AND s.source=?`, poolJSON(ids), source)
		if e != nil {
			return e
		}
		fresh := []int64{}
		desired := map[int64][]string{}
		for rows.Next() {
			var id int64
			var active sql.NullBool
			var excluded bool
			if e = rows.Scan(&id, &active, &excluded); e != nil {
				rows.Close()
				return e
			}
			switch {
			case excluded || states[id] == "excluded":
				result["skipped"]++
			case states[id] == "active" || (active.Valid && active.Bool):
				result["exists"]++
			case active.Valid:
				result["skipped"]++
			default:
				fresh = append(fresh, id)
				desired[id] = []string{source}
				result["added"]++
			}
		}
		e = rows.Err()
		rows.Close()
		if e != nil {
			return e
		}
		if len(fresh) > 0 {
			_, e = p.Monitor.ApplyTx(ctx, conn, "game", fresh, monitoring.Change{Sources: desired, AddOnly: true})
		}
		return e
	})
	if err != nil {
		return nil, err
	}
	p.Log("榜单发现源落池完成 │ 来源=%s，新增=%d，已在池=%d，已移除跳过=%d", source, result["added"], result["exists"], result["skipped"])
	return result, nil
}

func (p *Pool) RecordBoard(ctx context.Context, input []int64) (int, error) {
	ids := cleanPoolIDs(input)
	if len(ids) == 0 {
		return 0, nil
	}
	err := p.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		if _, e := conn.ExecContext(ctx, `DELETE FROM preset_games WHERE source='topsellers'`); e != nil {
			return e
		}
		_, e := conn.ExecContext(ctx, `INSERT INTO preset_games(appid,name,source,added_at) SELECT CAST(j.value AS INTEGER),g.name,'topsellers',? FROM json_each(?) j LEFT JOIN games g ON g.appid=j.value WHERE 1 ON CONFLICT(appid) DO UPDATE SET name=excluded.name,source=excluded.source,added_at=excluded.added_at`, time.Now().Format("2006-01-02 15:04:05.000000"), poolJSON(ids))
		return e
	})
	if err != nil {
		return 0, err
	}
	return len(ids), nil
}
