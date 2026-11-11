package wishlist

import (
	"context"
	"database/sql"
)

func (p *Pool) RemoveAccountsTx(ctx context.Context, conn *sql.Conn, accounts []string) (map[string]int, error) {
	result := map[string]int{"itemsRemoved": 0, "poolReleased": 0}
	if len(accounts) == 0 {
		return result, nil
	}
	raw := poolJSON(accounts)
	rows, e := conn.QueryContext(ctx, `SELECT appid FROM wishlist_items WHERE steamid IN(SELECT value FROM json_each(?))`, raw)
	if e != nil {
		return nil, e
	}
	ids := []int64{}
	for rows.Next() {
		var id int64
		if e = rows.Scan(&id); e != nil {
			rows.Close()
			return nil, e
		}
		ids = append(ids, id)
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return nil, e
	}
	result["itemsRemoved"] = len(ids)
	for _, table := range []string{"wishlist_items", "tracked_accounts"} {
		if _, e = conn.ExecContext(ctx, `DELETE FROM `+table+` WHERE steamid IN(SELECT value FROM json_each(?))`, raw); e != nil {
			return nil, e
		}
	}
	rows, e = conn.QueryContext(ctx, `SELECT DISTINCT target_id FROM monitor_sources s WHERE target_type='game' AND active=1 AND source IN('family_wishlist','owned') AND NOT EXISTS(SELECT 1 FROM wishlist_items w WHERE w.appid=s.target_id AND w.active=1)`)
	if e != nil {
		return nil, e
	}
	for rows.Next() {
		var id int64
		if e = rows.Scan(&id); e != nil {
			rows.Close()
			return nil, e
		}
		ids = append(ids, id)
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return nil, e
	}
	ids = cleanPoolIDs(ids)
	before, e := p.Monitor.StatesTx(ctx, conn, "game", ids)
	if e != nil {
		return nil, e
	}
	after, e := p.Monitor.SyncGameSourcesTx(ctx, conn, ids)
	if e != nil {
		return nil, e
	}
	for id, state := range after {
		if before[id] == "active" && state == "released" {
			result["poolReleased"]++
		}
	}
	return result, nil
}
