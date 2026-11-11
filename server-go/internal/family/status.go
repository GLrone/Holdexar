package family

import (
	"context"
	"database/sql"
	"encoding/json"
	"strings"
	"sync"
	"time"

	"holdexar/server-go/internal/account"
	"holdexar/server-go/internal/store"
)

func avatarMissing(raw string) bool {
	raw = strings.TrimSpace(raw)
	tail := raw[strings.LastIndex(raw, "/")+1:]
	return raw == "" || strings.HasPrefix(tail, ".") || strings.HasPrefix(tail, "_")
}
func memberName(m map[string]any) string {
	if v := str(m["personaName"]); v != "" {
		return v
	}
	return str(m["persona_name"])
}
func memberAvatar(m map[string]any) string {
	v := str(m["avatarUrl"])
	if v == "" {
		v = str(m["avatar_url"])
	}
	return account.NormalizeAvatar(v)
}
func (s *Service) groups(ctx context.Context) ([]*groupRow, error) {
	rows, e := s.db.QueryContext(ctx, `SELECT steamid,family_groupid,family_name,members_json,fetch_ok,last_error,member_count,CAST(updated_at AS TEXT) FROM family_groups ORDER BY steamid`)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	out := []*groupRow{}
	for rows.Next() {
		g := &groupRow{Members: []map[string]any{}}
		var raw sql.NullString
		if e = rows.Scan(&g.SID, &g.Group, &g.Name, &raw, &g.FetchOK, &g.Error, &g.Count, &g.Updated); e != nil {
			return nil, e
		}
		if raw.Valid && raw.String != "null" {
			if e = json.Unmarshal([]byte(raw.String), &g.Members); e != nil {
				return nil, e
			}
		}
		out = append(out, g)
	}
	return out, rows.Err()
}
func (s *Service) healGroup(ctx context.Context, g *groupRow) error {
	if g == nil {
		return nil
	}
	now := s.opts.Now()
	s.mu.Lock()
	avatarsAllowed := !now.Before(s.avatarUntil)
	countriesAllowed := !now.Before(s.countryUntil)
	s.mu.Unlock()
	profiles := map[string]map[string]any{}
	missing := []string{}
	countryIDs := []string{}
	for _, m := range g.Members {
		sid := str(m["steamid"])
		if sid == "" || yes(m["excluded"]) {
			continue
		}
		if (memberName(m) == "" || avatarMissing(memberAvatar(m))) && avatarsAllowed && s.opts.Profile != nil {
			missing = append(missing, sid)
		}
		if str(m["loccountrycode"]) == "" && countriesAllowed {
			countryIDs = append(countryIDs, sid)
		}
	}
	var wg sync.WaitGroup
	var mu sync.Mutex
	for _, sid := range missing {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if p, e := s.opts.Profile(ctx, sid); e == nil {
				mu.Lock()
				profiles[sid] = p
				mu.Unlock()
			}
		}()
	}
	wg.Wait()
	countries := map[string]string{}
	if len(countryIDs) > 0 {
		if found, e := s.countries(ctx, countryIDs); e == nil {
			countries = found
		}
	}
	if ctx.Err() != nil {
		return ctx.Err()
	}
	s.mu.Lock()
	if len(missing) > 0 {
		good := false
		for _, p := range profiles {
			good = good || !avatarMissing(str(p["avatar_url"]))
		}
		if !good {
			s.avatarUntil = now.Add(time.Minute)
		} else {
			s.avatarUntil = time.Time{}
		}
	}
	if len(countryIDs) > 0 && len(countries) == 0 {
		s.countryUntil = now.Add(10 * time.Minute)
	}
	s.mu.Unlock()
	if len(profiles) == 0 && len(countries) == 0 {
		return nil
	}
	return s.gate.WithWrite(ctx, store.PriorityBackground, func(c *sql.Conn) error {
		current, e := readGroup(ctx, c, g.SID)
		if e != nil || current == nil {
			return e
		}
		changed := false
		for _, m := range current.Members {
			sid := str(m["steamid"])
			if yes(m["excluded"]) {
				continue
			}
			p := profiles[sid]
			if memberName(m) == "" && str(p["persona_name"]) != "" {
				m["personaName"] = str(p["persona_name"])
				changed = true
			}
			if avatarMissing(memberAvatar(m)) && !avatarMissing(str(p["avatar_url"])) {
				m["avatarUrl"] = account.NormalizeAvatar(str(p["avatar_url"]))
				changed = true
			}
			if str(m["loccountrycode"]) == "" && countries[sid] != "" {
				m["loccountrycode"] = countries[sid]
				changed = true
			}
		}
		if !changed {
			return nil
		}
		return s.saveMembers(ctx, c, current)
	})
}
func (s *Service) memberRegions(ctx context.Context, accounts []boundRow, groups []*groupRow) (map[string]map[string]any, map[string]string, map[string]string, error) {
	valid := map[string]bool{}
	rows, e := s.db.QueryContext(ctx, `SELECT lower(code) FROM crawl_regions`)
	if e != nil {
		return nil, nil, nil, e
	}
	for rows.Next() {
		var code string
		if e = rows.Scan(&code); e != nil {
			rows.Close()
			return nil, nil, nil, e
		}
		valid[code] = true
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return nil, nil, nil, e
	}
	manual := map[string]string{}
	wallet := map[string]string{}
	profile := map[string]string{}
	sids := map[string]bool{}
	bound := map[string]bool{}
	for _, a := range accounts {
		bound[a.SID] = true
		sids[a.SID] = true
		code := strings.ToLower(str(a.Wallet["region_code"]))
		if valid[code] {
			wallet[a.SID] = code
		}
	}
	for _, g := range groups {
		if g == nil {
			continue
		}
		for _, m := range g.Members {
			sid := str(m["steamid"])
			if sid != "" {
				sids[sid] = true
				if p := strings.ToLower(str(m["loccountrycode"])); valid[p] && profile[sid] == "" {
					profile[sid] = p
				}
			}
		}
	}
	resolved := map[string]map[string]any{}
	e = s.gate.WithWrite(ctx, store.PriorityBackground, func(c *sql.Conn) error {
		if e := kv(ctx, c, regionsKey, &manual); e != nil {
			return e
		}
		if manual == nil {
			manual = map[string]string{}
		}
		changed := false
		if len(accounts) > 0 && len(manual) > 0 {
			done := false
			if e := kv(ctx, c, cleanupKey, &done); e != nil {
				return e
			}
			if !done {
				for sid, value := range manual {
					if value == "cn" && !bound[sid] {
						delete(manual, sid)
						changed = true
					}
				}
				if e := s.putKV(ctx, c, cleanupKey, true); e != nil {
					return e
				}
			}
		}
		for sid := range sids {
			m := strings.ToLower(strings.TrimSpace(manual[sid]))
			w, p := wallet[sid], profile[sid]
			derived := w
			if derived == "" {
				derived = p
			}
			if m != "" && m != "cn" {
				resolved[sid] = map[string]any{"region": m, "regionSource": "manual"}
				continue
			}
			if m == "cn" && derived != "" && derived != m {
				delete(manual, sid)
				m = ""
				changed = true
			}
			region, source := m, "manual"
			if w != "" {
				region, source = w, "wallet"
			} else if p != "" {
				region, source = p, "profile"
			}
			if region == "" {
				source = ""
			}
			resolved[sid] = map[string]any{"region": optional(region), "regionSource": optional(source)}
		}
		if changed {
			return s.putKV(ctx, c, regionsKey, manual)
		}
		return nil
	})
	return resolved, manual, wallet, e
}
func payload(g *groupRow, regions map[string]map[string]any) []map[string]any {
	out := []map[string]any{}
	seen := map[string]bool{}
	if g == nil {
		return out
	}
	for _, m := range g.Members {
		sid := str(m["steamid"])
		if sid == "" || yes(m["excluded"]) || seen[sid] {
			continue
		}
		seen[sid] = true
		r := regions[sid]
		origin := str(m["origin"])
		if origin == "" {
			origin = "steam"
		}
		out = append(out, map[string]any{"steamid": sid, "role": str(m["role"]), "personaName": memberName(m), "avatarUrl": memberAvatar(m), "origin": origin, "region": r["region"], "regionSource": r["regionSource"]})
	}
	return out
}
func (s *Service) Status(parent context.Context) (map[string]any, error) {
	ctx, done, e := s.work(parent)
	if e != nil {
		return nil, e
	}
	defer done()
	accounts, e := s.bound(ctx)
	if e != nil {
		return nil, e
	}
	stored, e := s.groups(ctx)
	if e != nil {
		return nil, e
	}
	byID := map[string]*groupRow{}
	ids := []string{}
	for _, g := range stored {
		byID[g.SID] = g
		ids = append(ids, g.SID)
	}
	all := []*groupRow{}
	if len(accounts) > 0 {
		for _, a := range accounts {
			if g := byID[a.SID]; g != nil {
				if e = s.healGroup(ctx, g); e != nil {
					return nil, e
				}
			}
		}
		stored, e = s.groups(ctx)
		if e != nil {
			return nil, e
		}
		byID = map[string]*groupRow{}
		for _, g := range stored {
			byID[g.SID] = g
		}
		for _, a := range accounts {
			if g := byID[a.SID]; g != nil {
				all = append(all, g)
			}
		}
		if byID[localGroup] != nil {
			all = append(all, byID[localGroup])
		}
	} else {
		for _, id := range ids {
			all = append(all, byID[id])
		}
	}
	regions, manual, wallet, e := s.memberRegions(ctx, accounts, all)
	if e != nil {
		return nil, e
	}
	groups := []map[string]any{}
	members := []map[string]any{}
	seen := map[string]bool{}
	addFlat := func(list []map[string]any) {
		for _, m := range list {
			sid := m["steamid"].(string)
			if !seen[sid] {
				seen[sid] = true
				members = append(members, m)
			}
		}
	}
	makeGroup := func(sid, name string, g *groupRow) map[string]any {
		m := payload(g, regions)
		var joined any
		var familyName, groupid, updated, lastError any
		synced := false
		if g != nil {
			synced = g.Error.String == ""
			if g.Error.String == "" || g.Count > 0 {
				joined = g.Count > 0
			}
			familyName = optional(g.Name.String)
			groupid = optional(g.Group.String)
			updated = iso(g.Updated)
			lastError = optional(g.Error.String)
		}
		return map[string]any{"steamid": sid, "accountName": name, "synced": synced, "joined": joined, "familyName": familyName, "familyGroupid": groupid, "updatedAt": updated, "lastError": lastError, "members": m}
	}
	if len(accounts) > 0 {
		for _, a := range accounts {
			g := makeGroup(a.SID, a.Name, byID[a.SID])
			groups = append(groups, g)
			addFlat(g["members"].([]map[string]any))
		}
		local := payload(byID[localGroup], regions)
		for _, m := range local {
			if !seen[m["steamid"].(string)] {
				groups[0]["members"] = append(groups[0]["members"].([]map[string]any), m)
				addFlat([]map[string]any{m})
			}
		}
	} else {
		for _, id := range ids {
			g := byID[id]
			if len(payload(g, regions)) == 0 {
				continue
			}
			v := makeGroup(id, "", g)
			v["synced"] = true
			if g.Group.String == "" {
				v["joined"] = nil
			}
			groups = append(groups, v)
			addFlat(v["members"].([]map[string]any))
		}
	}
	primary := ""
	var message, lastError any
	joined := false
	var walletRegion any
	if len(accounts) > 0 {
		primary = accounts[0].SID
		walletRegion = optional(wallet[primary])
		if g := byID[primary]; g != nil {
			lastError = optional(g.Error.String)
		}
		for _, g := range groups {
			joined = joined || g["joined"] == true
		}
	} else {
		message = "尚未绑定 Steam 账号（请在「我」页绑定后同步家庭组）"
	}
	return map[string]any{"bound": len(accounts) > 0, "primarySteamid": primary, "steamid": primary, "joined": joined, "walletRegion": walletRegion, "memberRegions": manual, "groups": groups, "members": members, "message": message, "lastError": lastError}, nil
}
