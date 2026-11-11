package wishlist

import (
	"context"
	"database/sql"
	"encoding/json"
	"math/big"
	"strings"

	"holdexar/server-go/internal/store"
)

type InputError struct{ Message string }

func (e *InputError) Error() string { return e.Message }
func friendCode(sid string) any {
	n, ok := new(big.Int).SetString(sid, 10)
	if !ok {
		return nil
	}
	n.Sub(n, big.NewInt(steamBase))
	if n.Sign() < 0 {
		return nil
	}
	return n.String()
}
func optionalText(v string) any {
	if v == "" {
		return nil
	}
	return v
}
func viewKinds(kinds map[string]bool) map[string]bool {
	if len(kinds) == 0 {
		return map[string]bool{"wishlist": true, "owned": false}
	}
	return kinds
}
func (s *Service) Accounts(ctx context.Context) ([]map[string]any, error) {
	rows, e := s.pool.DB.QueryContext(ctx, `SELECT a.steamid,a.label,a.persona_name,a.avatar_url,a.kinds_json,CAST(a.last_sync_at AS TEXT),COALESCE(c.total,0),COALESCE(c.owned,0) FROM tracked_accounts a LEFT JOIN (SELECT steamid,COUNT(*) total,SUM(owned) owned FROM wishlist_items WHERE active=1 GROUP BY steamid) c ON c.steamid=a.steamid ORDER BY a.created_at DESC,a.steamid`)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	out := []map[string]any{}
	for rows.Next() {
		var sid string
		var label, name, avatar, kinds, at sql.NullString
		var total, owned int
		if e = rows.Scan(&sid, &label, &name, &avatar, &kinds, &at, &total, &owned); e != nil {
			return nil, e
		}
		var flags map[string]bool
		if kinds.Valid && kinds.String != "" {
			if e = json.Unmarshal([]byte(kinds.String), &flags); e != nil {
				return nil, e
			}
		}
		stamp, e := poolISO(at)
		if e != nil {
			return nil, e
		}
		out = append(out, map[string]any{"steamid": sid, "label": optionalText(label.String), "personaName": optionalText(strings.TrimSpace(name.String)), "avatarUrl": optionalText(s.opts.Avatar(avatar.String)), "friendCode": friendCode(sid), "kinds": viewKinds(flags), "lastSyncAt": stamp, "itemCount": total, "ownedCount": owned})
	}
	return out, rows.Err()
}
func (s *Service) AddAccount(parent context.Context, raw, label string, kinds map[string]bool) (map[string]any, error) {
	ctx, done, e := s.work(parent)
	if e != nil {
		return nil, e
	}
	defer done()
	sid, e := s.opts.Steam.Resolve(ctx, raw)
	if e != nil {
		return nil, &InputError{e.Error()}
	}
	profile := map[string]any{}
	if s.opts.Persona != nil {
		if v, e := s.opts.Persona(ctx, sid); e == nil {
			profile = v
		}
	}
	e = s.pool.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		var exists bool
		if e := conn.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM tracked_accounts WHERE steamid=?)`, sid).Scan(&exists); e != nil {
			return e
		}
		if exists {
			return &InputError{"账户已存在: " + sid}
		}
		_, e := conn.ExecContext(ctx, `INSERT INTO tracked_accounts(steamid,label,persona_name,avatar_url,kinds_json,created_at) VALUES(?,?,?,?,?,?)`, sid, optionalText(label), optionalText(text(profile["persona_name"])), optionalText(s.opts.Avatar(text(profile["avatar_url"]))), poolJSON(syncKinds(kinds)), trackedStamp(s.now()))
		return e
	})
	if e != nil {
		return nil, e
	}
	s.pool.Log("已绑定 Steam 追踪账户 │ SteamID=%s", sid)
	s.RequestSync(sid, true)
	return map[string]any{"steamid": sid, "friendCode": friendCode(sid)}, nil
}
func (s *Service) UpdateKinds(ctx context.Context, sid string, patch map[string]bool) (map[string]any, error) {
	var flags map[string]bool
	e := s.pool.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		row, e := s.tracked(ctx, conn, sid)
		if e != nil {
			return e
		}
		flags = syncKinds(row.Kinds)
		for _, key := range []string{"wishlist", "owned"} {
			if v, ok := patch[key]; ok {
				flags[key] = v
			}
		}
		_, e = conn.ExecContext(ctx, `UPDATE tracked_accounts SET kinds_json=? WHERE steamid=?`, poolJSON(flags), sid)
		return e
	})
	if e != nil {
		return nil, e
	}
	s.pool.Log("账户同步类型已更新 │ SteamID=%s，愿望单=%t，已购=%t", sid, flags["wishlist"], flags["owned"])
	return map[string]any{"steamid": sid, "kinds": flags}, nil
}
func (s *Service) RemoveAccount(ctx context.Context, sid string) (map[string]any, error) {
	var result map[string]int
	e := s.pool.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		if _, e := s.tracked(ctx, conn, sid); e != nil {
			return e
		}
		var e error
		result, e = s.pool.RemoveAccountsTx(ctx, conn, []string{sid})
		return e
	})
	if e != nil {
		return nil, e
	}
	s.pool.Log("已解绑 Steam 追踪账户 │ SteamID=%s，条目移除=%d，监控回收=%d", sid, result["itemsRemoved"], result["poolReleased"])
	return map[string]any{"removed": true, "steamid": sid, "itemsRemoved": result["itemsRemoved"], "poolReleased": result["poolReleased"], "reconciled": true}, nil
}
