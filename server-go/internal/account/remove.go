package account

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"

	"holdexar/server-go/internal/store"
)

func (s *Service) RemoveAccount(parent context.Context, sid string) (map[string]any, error) {
	ctx, done, e := s.work(parent)
	if e != nil {
		return nil, e
	}
	defer done()
	unlock, e := s.lock(ctx, sid)
	if e != nil {
		return nil, e
	}
	defer unlock()
	return s.removeAccounts(ctx, &sid)
}

func (s *Service) ClearCookies(parent context.Context) error {
	ctx, done, e := s.work(parent)
	if e != nil {
		return e
	}
	defer done()
	_, e = s.removeAccounts(ctx, nil)
	return e
}

func (s *Service) removeAccounts(ctx context.Context, sid *string) (map[string]any, error) {
	result := map[string]any{"ok": true}
	ids := []string{}
	e := s.gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		query := `SELECT steam_id FROM steam_accounts`
		args := []any{}
		if sid != nil {
			query += ` WHERE steam_id=?`
			args = append(args, *sid)
		}
		rows, e := conn.QueryContext(ctx, query, args...)
		if e != nil {
			return e
		}
		for rows.Next() {
			var id string
			if e = rows.Scan(&id); e != nil {
				rows.Close()
				return e
			}
			ids = append(ids, id)
		}
		e = rows.Err()
		rows.Close()
		if e != nil {
			return e
		}
		if sid != nil && len(ids) == 0 {
			return fmt.Errorf("%w: %s", ErrAccountNotFound, *sid)
		}
		raw, _ := json.Marshal(ids)
		if len(ids) > 0 {
			for _, table := range []string{"bill_game_txs", "bill_topup_txs", "bill_cdk_games"} {
				if _, e = conn.ExecContext(ctx, `DELETE FROM `+table+` WHERE import_id IN(SELECT id FROM bill_imports WHERE steam_id IN(SELECT value FROM json_each(?)))`, string(raw)); e != nil {
					return e
				}
			}
			for _, item := range []struct{ table, owner, metric string }{
				{"bill_imports", "steam_id", "billsRemoved"}, {"family_groups", "steamid", "familyGroupsRemoved"},
			} {
				deleted, e := conn.ExecContext(ctx, `DELETE FROM `+item.table+` WHERE `+item.owner+` IN(SELECT value FROM json_each(?))`, string(raw))
				if e != nil {
					return e
				}
				n, e := deleted.RowsAffected()
				if e != nil {
					return e
				}
				result[item.metric] = int(n)
			}
			deleted, e := conn.ExecContext(ctx, `DELETE FROM family_library_snapshots AS s WHERE NOT EXISTS(SELECT 1 FROM family_groups g WHERE g.family_groupid=s.family_groupid)`)
			if e != nil {
				return e
			}
			n, e := deleted.RowsAffected()
			if e != nil {
				return e
			}
			result["familySnapshotsRemoved"] = int(n)
			tracking, e := s.opts.Pool.RemoveAccountsTx(ctx, conn, ids)
			if e != nil {
				return e
			}
			result["trackingRemoved"], result["poolReleased"] = tracking["itemsRemoved"], tracking["poolReleased"]
			if _, e = conn.ExecContext(ctx, `DELETE FROM steam_accounts WHERE steam_id IN(SELECT value FROM json_each(?))`, string(raw)); e != nil {
				return e
			}
			if _, e = conn.ExecContext(ctx, `UPDATE steam_accounts SET is_active=(steam_id=(SELECT steam_id FROM steam_accounts ORDER BY bound_at,steam_id LIMIT 1)) WHERE NOT EXISTS(SELECT 1 FROM steam_accounts WHERE is_active=1)`); e != nil {
				return e
			}
		}
		if sid == nil {
			if _, e = conn.ExecContext(ctx, `INSERT INTO app_settings(key,value_json,updated_at) VALUES(?,'""',?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at`, legacyCookiesKey, accountStamp(s.now())); e != nil {
				return e
			}
		}
		var remaining int
		if e = conn.QueryRowContext(ctx, `SELECT count(*) FROM steam_accounts`).Scan(&remaining); e != nil {
			return e
		}
		result["remaining"] = remaining
		return nil
	})
	if e != nil {
		return nil, e
	}
	s.mu.Lock()
	for _, id := range ids {
		delete(s.renewAt, id)
	}
	s.mu.Unlock()
	if sid != nil {
		result["removed"] = *sid
	}
	s.opts.Log("Steam 账号解绑已保存 │ SteamID=%v，剩余=%v，账单=%v，追踪=%v，监控回收=%v", ids, result["remaining"], result["billsRemoved"], result["trackingRemoved"], result["poolReleased"])
	return result, nil
}
