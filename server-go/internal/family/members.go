package family

import (
	"context"
	"database/sql"
	"strings"

	"holdexar/server-go/internal/account"
	"holdexar/server-go/internal/events"
	"holdexar/server-go/internal/store"
)

func (s *Service) saveMembers(ctx context.Context, c *sql.Conn, g *groupRow) error {
	_, e := c.ExecContext(ctx, `INSERT INTO family_groups(steamid,members_json,fetch_ok,member_count,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(steamid) DO UPDATE SET members_json=excluded.members_json,updated_at=excluded.updated_at`, g.SID, packed(g.Members), g.FetchOK, g.Count, s.stamp())
	return e
}
func (s *Service) AddMember(ctx context.Context, sid, name, avatar, requested string) (map[string]any, error) {
	ctx, done, e := s.work(ctx)
	if e != nil {
		return nil, e
	}
	defer done()
	sid = strings.TrimSpace(sid)
	if !validSID(sid) {
		return nil, &InputError{"无效的 SteamID64，请先通过输入框解析好友码"}
	}
	owner, e := s.owner(ctx, requested)
	if e != nil {
		return nil, e
	}
	receipt := map[string]any{"ok": true}
	e = s.gate.WithWrite(ctx, store.PriorityInteractive, func(c *sql.Conn) error {
		g, e := readGroup(ctx, c, owner)
		if e != nil {
			return e
		}
		if g == nil {
			g = &groupRow{SID: owner, Members: []map[string]any{}}
		}
		if owner != localGroup {
			local, e := readGroup(ctx, c, localGroup)
			if e != nil {
				return e
			}
			if local != nil {
				kept := []map[string]any{}
				for _, m := range local.Members {
					if str(m["steamid"]) != sid || yes(m["excluded"]) {
						kept = append(kept, m)
					}
				}
				if len(kept) != len(local.Members) {
					local.Members = kept
					if e = s.saveMembers(ctx, c, local); e != nil {
						return e
					}
				}
			}
		}
		found, restored := false, false
		for _, m := range g.Members {
			if str(m["steamid"]) == sid {
				found = true
				if yes(m["excluded"]) {
					delete(m, "excluded")
					restored = true
				}
			}
		}
		if restored {
			receipt["reactivated"] = true
			return s.saveMembers(ctx, c, g)
		}
		if found {
			receipt["duplicate"] = true
			return nil
		}
		g.Members = append(g.Members, map[string]any{"steamid": sid, "role": "", "origin": "manual", "personaName": strings.TrimSpace(name), "avatarUrl": account.NormalizeAvatar(avatar), "loccountrycode": nil})
		return s.saveMembers(ctx, c, g)
	})
	if e != nil {
		return nil, e
	}
	s.opts.Log("家庭成员已添加 │ SteamID=%s，来源组=%s", sid, owner)
	return receipt, nil
}
func (s *Service) RemoveMember(ctx context.Context, sid, requested string) (map[string]any, error) {
	ctx, done, e := s.work(ctx)
	if e != nil {
		return nil, e
	}
	defer done()
	sid = strings.TrimSpace(sid)
	owner, e := s.owner(ctx, requested)
	if e != nil {
		return nil, e
	}
	removed := false
	e = s.gate.WithWrite(ctx, store.PriorityInteractive, func(c *sql.Conn) error {
		g, e := readGroup(ctx, c, owner)
		if e != nil {
			return e
		}
		if owner != localGroup {
			var primary string
			if e = c.QueryRowContext(ctx, `SELECT COALESCE((SELECT steam_id FROM steam_accounts ORDER BY bound_at,steam_id LIMIT 1),(SELECT json_extract(value_json,'$') FROM app_settings WHERE key='account.steam_id'),'local')`).Scan(&primary); e != nil {
				return e
			}
			if owner == primary {
				local, e := readGroup(ctx, c, localGroup)
				if e != nil {
					return e
				}
				if removeFromGroup(local, sid) {
					if e = s.saveMembers(ctx, c, local); e != nil {
						return e
					}
					removed = true
				}
			}
		}
		if g == nil && !removed {
			return &InputError{"该账号还没有家庭组记录，没有可移除的成员"}
		}
		if removeFromGroup(g, sid) {
			if e = s.saveMembers(ctx, c, g); e != nil {
				return e
			}
			removed = true
		}
		return nil
	})
	if e != nil {
		return nil, e
	}
	s.opts.Log("家庭成员移除已处理 │ SteamID=%s，来源组=%s，移除=%t", sid, owner, removed)
	return map[string]any{"ok": true, "removed": removed}, nil
}
func removeFromGroup(g *groupRow, sid string) bool {
	if g == nil {
		return false
	}
	kept := []map[string]any{}
	changed := false
	for _, m := range g.Members {
		if str(m["steamid"]) == sid {
			if str(m["origin"]) == "manual" {
				changed = true
				continue
			}
			if !yes(m["excluded"]) {
				m["excluded"] = true
				changed = true
			}
		}
		kept = append(kept, m)
	}
	g.Members = kept
	return changed
}
func (s *Service) SaveRegions(ctx context.Context, patch map[string]string) (map[string]string, error) {
	ctx, done, e := s.work(ctx)
	if e != nil {
		return nil, e
	}
	defer done()
	merged := map[string]string{}
	e = s.gate.WithWrite(ctx, store.PriorityInteractive, func(c *sql.Conn) error {
		if e := kv(ctx, c, regionsKey, &merged); e != nil {
			return e
		}
		if merged == nil {
			merged = map[string]string{}
		}
		for sid, v := range patch {
			if v = strings.TrimSpace(v); v != "" {
				merged[sid] = v
			} else {
				delete(merged, sid)
			}
		}
		return s.putKV(ctx, c, regionsKey, merged)
	})
	if e != nil {
		return nil, e
	}
	if s.opts.Bus != nil {
		s.opts.Bus.Publish("settings.changed", events.KV{Key: "keys", Val: []string{regionsKey}})
	}
	return merged, nil
}
