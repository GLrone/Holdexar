package wishlist

import (
	"context"
	"database/sql"
	"encoding/json"
	"sort"
	"strings"
	"time"

	"holdexar/server-go/internal/monitoring"
	"holdexar/server-go/internal/store"
)

type Pool struct {
	DB      *sql.DB
	Gate    *store.WriteGate
	Monitor *monitoring.Service
	OnAdded func(context.Context, []int64) (bool, error)
	Log     func(string, ...any)
}

func NewPool(db *sql.DB, gate *store.WriteGate, monitor *monitoring.Service) *Pool {
	return &Pool{DB: db, Gate: gate, Monitor: monitor, Log: func(string, ...any) {}}
}
func poolJSON(v any) string { b, _ := json.Marshal(v); return string(b) }
func cleanPoolIDs(ids []int64) []int64 {
	out := []int64{}
	seen := map[int64]bool{}
	for _, id := range ids {
		if id > 0 && !seen[id] {
			seen[id] = true
			out = append(out, id)
		}
	}
	return out
}
func hasSource(sources []string, want string) bool {
	for _, s := range sources {
		if s == want {
			return true
		}
	}
	return false
}

func (p *Pool) Follow(ctx context.Context, id int64, followed bool) (map[string]any, error) {
	off := false
	change := monitoring.Change{Sources: map[int64][]string{id: {"favorite"}}, AddOnly: true, Excluded: &off}
	if !followed {
		change = monitoring.Change{Sources: map[int64][]string{}, Managed: []string{"favorite"}}
	}
	err := p.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		if _, e := p.Monitor.ApplyTx(ctx, conn, "game", []int64{id}, change); e != nil {
			return e
		}
		if !followed {
			_, e := conn.ExecContext(ctx, "UPDATE wishlist_items SET manual=0 WHERE appid=? AND manual=1", id)
			return e
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	p.Log("游戏关注已更新 │ AppID=%d，关注=%t", id, followed)
	return map[string]any{"appid": id, "followed": followed}, nil
}

func (p *Pool) Add(ctx context.Context, input []int64, source string) (map[string]any, error) {
	results := []map[string]any{}
	for _, id := range input {
		if id <= 0 {
			results = append(results, map[string]any{"appid": id, "status": "fail", "detail": "AppID 无效"})
		}
	}
	ids := cleanPoolIDs(input)
	added, restored, exists := 0, 0, 0
	touched := []int64{}
	if len(ids) > 0 {
		err := p.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
			before, e := p.Monitor.StatesTx(ctx, conn, "game", ids)
			if e != nil {
				return e
			}
			sources, e := p.Monitor.SourcesTx(ctx, conn, "game", ids)
			if e != nil {
				return e
			}
			desired := map[int64][]string{}
			for _, id := range ids {
				desired[id] = []string{"manual"}
			}
			off := false
			if _, e = p.Monitor.ApplyTx(ctx, conn, "game", ids, monitoring.Change{Sources: desired, AddOnly: true, Excluded: &off}); e != nil {
				return e
			}
			for _, id := range ids {
				status, detail := "added", "已加入关注"
				if before[id] == "excluded" {
					status, detail = "restored", "已恢复关注"
					restored++
					touched = append(touched, id)
				} else if hasSource(sources[id], "manual") || before[id] == "active" {
					status, detail = "exists", "已在关注"
					exists++
				} else {
					added++
					touched = append(touched, id)
				}
				results = append(results, map[string]any{"appid": id, "status": status, "detail": detail})
			}
			return nil
		})
		if err != nil {
			return nil, err
		}
	}
	triggered := false
	if len(touched) > 0 && p.OnAdded != nil {
		var e error
		triggered, e = p.OnAdded(ctx, touched)
		if e != nil {
			p.Log("新增关注已保存，首次价格更新将由后续自动更新接续 │ AppID=%s，原因=%v", poolJSON(touched), e)
		}
	}
	if strings.TrimSpace(source) != "" && len(ids) > 0 {
		if e := p.recordImported(ctx, ids, source); e != nil {
			p.Log("本机导入来源登记失败，关注已保留：%v", e)
		}
	}
	p.Log("批量关注已保存 │ 对象=%s，新增=%d，恢复=%d，已有=%d", poolJSON(ids), added, restored, exists)
	return map[string]any{"results": results, "added": added, "restored": restored, "exists": exists, "fail": len(results) - len(ids), "crawlTriggered": triggered}, nil
}

func (p *Pool) recordImported(ctx context.Context, ids []int64, source string) error {
	runes := []rune(strings.TrimSpace(source))
	if len(runes) > 75 {
		runes = runes[:75]
	}
	name := "file:" + string(runes)
	return p.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		_, e := conn.ExecContext(ctx, `INSERT INTO preset_games(appid,name,source,added_at)
 SELECT CAST(j.value AS INTEGER),g.name,?,? FROM json_each(?) j LEFT JOIN games g ON g.appid=j.value WHERE 1
 ON CONFLICT(appid) DO NOTHING`, name, time.Now().Format("2006-01-02 15:04:05.000000"), poolJSON(ids))
		return e
	})
}

func (p *Pool) Remove(ctx context.Context, input []int64) (map[string]any, error) {
	ids := cleanPoolIDs(input)
	results := []map[string]any{}
	removed, missing := 0, 0
	if len(ids) > 0 {
		err := p.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
			states, e := p.Monitor.StatesTx(ctx, conn, "game", ids)
			if e != nil {
				return e
			}
			sources, e := p.Monitor.SourcesTx(ctx, conn, "game", ids)
			if e != nil {
				return e
			}
			hadActive := map[int64]bool{}
			rows, e := conn.QueryContext(ctx, "SELECT DISTINCT appid FROM wishlist_items WHERE active=1 AND appid IN(SELECT value FROM json_each(?))", poolJSON(ids))
			if e != nil {
				return e
			}
			for rows.Next() {
				var id int64
				if e = rows.Scan(&id); e != nil {
					rows.Close()
					return e
				}
				hadActive[id] = true
			}
			e = rows.Err()
			rows.Close()
			if e != nil {
				return e
			}
			if _, e = conn.ExecContext(ctx, `UPDATE wishlist_items SET active=0,excluded=1,manual=0 WHERE appid IN(SELECT value FROM json_each(?))`, poolJSON(ids)); e != nil {
				return e
			}
			on := true
			if _, e = p.Monitor.ApplyTx(ctx, conn, "game", ids, monitoring.Change{Sources: map[int64][]string{}, Managed: []string{"manual", "favorite", "board"}, Excluded: &on, Reason: "pool_removed"}); e != nil {
				return e
			}
			for _, id := range ids {
				status, detail := "missing", "不在关注列表"
				if states[id] == "active" || hadActive[id] || hasSource(sources[id], "manual") || hasSource(sources[id], "favorite") || hasSource(sources[id], "board") {
					status, detail = "removed", "已移出关注"
					removed++
				} else {
					missing++
				}
				results = append(results, map[string]any{"appid": id, "status": status, "detail": detail})
			}
			return nil
		})
		if err != nil {
			return nil, err
		}
	}
	p.Log("关注移除已保存 │ 对象=%s，已移除=%d", poolJSON(ids), removed)
	return map[string]any{"results": results, "removed": removed, "missing": missing}, nil
}

func poolISO(raw sql.NullString) (any, error) {
	if !raw.Valid || raw.String == "" {
		return nil, nil
	}
	for _, layout := range []string{"2006-01-02 15:04:05.999999999", "2006-01-02T15:04:05.999999999", time.RFC3339Nano} {
		if at, e := time.Parse(layout, raw.String); e == nil {
			format := "2006-01-02T15:04:05"
			if at.Nanosecond() != 0 {
				format += ".000000"
			}
			return at.Format(format), nil
		}
	}
	return raw.String, nil
}
func nullPoolString(s sql.NullString) any {
	if s.Valid {
		return s.String
	}
	return nil
}
func poolItem(id int64, added, name, en, header sql.NullString) (map[string]any, error) {
	at, e := poolISO(added)
	if e != nil {
		return nil, e
	}
	return map[string]any{"appid": id, "addedAt": at, "name": nullPoolString(name), "nameEn": nullPoolString(en), "headerImage": nullPoolString(header), "steamids": []string{}, "wishlisted": false, "followed": false, "owned": false, "manualPool": false, "boardPool": false}, nil
}

func (p *Pool) Items(ctx context.Context, steamid *string) ([]map[string]any, error) {
	query := `SELECT w.appid,w.steamid,CAST(w.added_at AS TEXT),w.wishlisted,w.manual,w.owned,w.manual_pool,w.board_pool,g.name,g.name_en,g.header_image FROM wishlist_items w LEFT JOIN games g ON g.appid=w.appid WHERE w.active=1`
	args := []any{}
	if steamid != nil && *steamid != "" {
		query += " AND w.steamid=?"
		args = append(args, *steamid)
	}
	query += " ORDER BY w.appid"
	rows, e := p.DB.QueryContext(ctx, query, args...)
	if e != nil {
		return nil, e
	}
	merged := map[int64]map[string]any{}
	for rows.Next() {
		var id int64
		var sid string
		var added, name, en, header sql.NullString
		var wished, manual, owned, manualPool, boardPool bool
		if e = rows.Scan(&id, &sid, &added, &wished, &manual, &owned, &manualPool, &boardPool, &name, &en, &header); e != nil {
			rows.Close()
			return nil, e
		}
		item := merged[id]
		if item == nil {
			item, e = poolItem(id, added, name, en, header)
			if e != nil {
				rows.Close()
				return nil, e
			}
			merged[id] = item
		}
		item["steamids"] = append(item["steamids"].([]string), sid)
		for key, value := range map[string]bool{"wishlisted": wished, "followed": manual, "owned": owned, "manualPool": manualPool, "boardPool": boardPool} {
			item[key] = item[key].(bool) || value
		}
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return nil, e
	}
	if steamid == nil {
		rows, e = p.DB.QueryContext(ctx, `SELECT t.target_id,CAST(t.created_at AS TEXT),g.name,g.name_en,g.header_image FROM monitor_targets t LEFT JOIN games g ON g.appid=t.target_id WHERE t.target_type='game' AND (`+monitoring.StateExpression+`)='active' ORDER BY t.target_id`)
		if e != nil {
			return nil, e
		}
		active := []int64{}
		for rows.Next() {
			var id int64
			var added, name, en, header sql.NullString
			if e = rows.Scan(&id, &added, &name, &en, &header); e != nil {
				rows.Close()
				return nil, e
			}
			active = append(active, id)
			if merged[id] == nil {
				merged[id], e = poolItem(id, added, name, en, header)
				if e != nil {
					rows.Close()
					return nil, e
				}
			}
		}
		e = rows.Err()
		rows.Close()
		if e != nil {
			return nil, e
		}
		sources, e := p.Monitor.Sources(ctx, "game", active)
		if e != nil {
			return nil, e
		}
		for id, names := range sources {
			item := merged[id]
			for field, source := range map[string]string{"wishlisted": "family_wishlist", "followed": "favorite", "owned": "owned", "manualPool": "manual", "boardPool": "board"} {
				item[field] = item[field].(bool) || hasSource(names, source)
			}
		}
		ids := []int64{}
		for id := range merged {
			ids = append(ids, id)
		}
		states, e := p.Monitor.States(ctx, "game", ids)
		if e != nil {
			return nil, e
		}
		for id, state := range states {
			if state == "excluded" {
				delete(merged, id)
			}
		}
	}
	ids := make([]int64, 0, len(merged))
	for id := range merged {
		ids = append(ids, id)
	}
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
	out := make([]map[string]any, 0, len(ids))
	for _, id := range ids {
		out = append(out, merged[id])
	}
	return out, nil
}

func (p *Pool) AppIDs(ctx context.Context) (map[string]any, error) {
	rows, e := p.DB.QueryContext(ctx, `WITH states AS(SELECT t.target_id,`+monitoring.StateExpression+` AS state FROM monitor_targets t WHERE target_type='game'),ids AS(SELECT appid FROM wishlist_items WHERE active=1 UNION SELECT target_id FROM states WHERE state='active')
 SELECT appid FROM ids WHERE appid NOT IN(SELECT target_id FROM states WHERE state IN('excluded','released')) ORDER BY appid`)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	ids := []int64{}
	for rows.Next() {
		var id int64
		if e = rows.Scan(&id); e != nil {
			return nil, e
		}
		ids = append(ids, id)
	}
	if e = rows.Err(); e != nil {
		return nil, e
	}
	return map[string]any{"appids": ids, "total": len(ids)}, nil
}

func (p *Pool) ReleaseFree(ctx context.Context, input []int64) (int, error) {
	ids := cleanPoolIDs(input)
	if len(ids) == 0 {
		return 0, nil
	}
	released := 0
	free := []int64{}
	err := p.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		rows, e := conn.QueryContext(ctx, `SELECT appid FROM games WHERE free_kind='f2p' AND appid IN(SELECT value FROM json_each(?)) ORDER BY appid`, poolJSON(ids))
		if e != nil {
			return e
		}
		for rows.Next() {
			var id int64
			if e = rows.Scan(&id); e != nil {
				rows.Close()
				return e
			}
			free = append(free, id)
		}
		e = rows.Err()
		rows.Close()
		if e != nil || len(free) == 0 {
			return e
		}
		if e = conn.QueryRowContext(ctx, `SELECT count(DISTINCT appid) FROM wishlist_items WHERE active=1 AND appid IN(SELECT value FROM json_each(?))`, poolJSON(free)).Scan(&released); e != nil {
			return e
		}
		if _, e = conn.ExecContext(ctx, `UPDATE wishlist_items SET active=0,excluded=1,manual=0 WHERE appid IN(SELECT value FROM json_each(?))`, poolJSON(free)); e != nil {
			return e
		}
		_, e = p.Monitor.ApplyTx(ctx, conn, "game", free, monitoring.Change{Sources: map[int64][]string{}})
		return e
	})
	if err == nil && len(free) > 0 {
		p.Log("永久免费游戏已停止价格跟踪 │ 对象=%s", poolJSON(free))
	}
	return released, err
}
