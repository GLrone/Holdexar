package family

import (
	"context"
	"database/sql"
	"fmt"
	"sync"

	"holdexar/server-go/internal/account"
	"holdexar/server-go/internal/store"
)

var errIdentityChanged = fmt.Errorf("家庭同步期间账号已解绑或登录信息已更新")

func (s *Service) RequestSync(sid string) bool {
	s.mu.Lock()
	if s.closing {
		s.mu.Unlock()
		return false
	}
	if s.active[sid] {
		s.pending[sid] = true
		s.mu.Unlock()
		return true
	}
	s.active[sid] = true
	s.jobs.Add(1)
	s.mu.Unlock()
	go func() {
		defer s.jobs.Done()
		defer s.finish(sid)
		if _, e := s.syncOne(s.life, sid); e != nil && s.life.Err() == nil {
			s.opts.Log("家庭组自动同步未完成 │ SteamID=%s，原因=%v", sid, e)
		}
	}()
	return true
}
func (s *Service) finish(sid string) {
	s.mu.Lock()
	delete(s.active, sid)
	again := s.pending[sid]
	delete(s.pending, sid)
	s.mu.Unlock()
	if again {
		s.RequestSync(sid)
	}
}
func (s *Service) Sync(parent context.Context, requested string) (map[string]any, error) {
	ctx, done, e := s.work(parent)
	if e != nil {
		return nil, e
	}
	defer done()
	targets, e := s.bound(ctx)
	if e != nil {
		return nil, e
	}
	if requested != "" {
		selected := []boundRow{}
		for _, a := range targets {
			if a.SID == requested {
				selected = append(selected, a)
			}
		}
		if len(selected) == 0 {
			return nil, &InputError{"该账号无可用登录 Cookie，请先绑定"}
		}
		targets = selected
	}
	if len(targets) == 0 {
		return nil, &InputError{"尚未绑定 Steam Cookie，无法发现家庭组（请先在「我」页绑定）"}
	}
	results := []map[string]any{}
	joined, failed := 0, 0
	for _, a := range targets {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		s.mu.Lock()
		busy := s.active[a.SID]
		if !busy {
			s.active[a.SID] = true
		}
		s.mu.Unlock()
		if busy {
			results = append(results, map[string]any{"steamid": a.SID, "inflight": true, "joined": false})
			continue
		}
		v, e := s.syncOne(ctx, a.SID)
		s.finish(a.SID)
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		if e != nil {
			if requested != "" {
				return nil, e
			}
			s.opts.Log("该账号家庭组同步未完成 │ SteamID=%s，原因=%v", a.SID, e)
			v = map[string]any{"steamid": a.SID, "joined": false, "error": "家庭组同步未完成，请重试同步"}
		}
		if v["joined"] == true {
			joined++
		}
		if v["error"] != nil {
			failed++
		}
		results = append(results, v)
	}
	return map[string]any{"results": results, "synced": len(results), "joined": joined, "failed": failed}, nil
}
func (s *Service) stableToken(ctx context.Context, sid string) (string, bindingVersion, error) {
	if s.opts.Token == nil {
		return "", bindingVersion{}, &InputError{"Cookie 中无法提取 webapi_token，请重新绑定 Steam Cookie"}
	}
	var token string
	var version bindingVersion
	var e error
	for attempt := 0; attempt < 2; attempt++ {
		before, readErr := readIdentity(ctx, s.db, sid)
		if readErr != nil {
			return "", bindingVersion{}, readErr
		}
		token, e = s.opts.Token(ctx, sid)
		after, readErr := readIdentity(ctx, s.db, sid)
		if readErr != nil {
			return "", bindingVersion{}, readErr
		}
		if before == after {
			version = before
			break
		}
		if attempt == 1 {
			return "", bindingVersion{}, errIdentityChanged
		}
	}
	return token, version, e
}

func (s *Service) syncOne(ctx context.Context, sid string) (map[string]any, error) {
	token, version, e := s.stableToken(ctx, sid)
	if e != nil && version == (bindingVersion{}) {
		return nil, e
	}
	var group remoteGroup
	if e == nil && token != "" {
		group, e = s.fetchGroup(ctx, token)
	} else {
		e = fmt.Errorf("该账号登录凭据暂不可用")
	}
	if e != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		failure := "家庭组同步未完成，请重试同步"
		if writeErr := s.gate.WithWrite(ctx, store.PriorityBackground, func(c *sql.Conn) error {
			if e := checkIdentity(ctx, c, sid, version); e != nil {
				return e
			}
			_, e := c.ExecContext(ctx, `INSERT INTO family_groups(steamid,members_json,fetch_ok,last_error,member_count,updated_at) VALUES(?,'[]',0,?,0,?) ON CONFLICT(steamid) DO UPDATE SET fetch_ok=0,last_error=excluded.last_error,updated_at=excluded.updated_at`, sid, failure, s.stamp())
			return e
		}); writeErr != nil {
			return nil, writeErr
		}
		s.opts.Log("该账号家庭组同步失败，原成员已保留 │ SteamID=%s，原因=%v", sid, e)
		return map[string]any{"steamid": sid, "joined": false, "error": failure}, nil
	}
	ids := []string{}
	for _, m := range group.Members {
		ids = append(ids, str(m["steamid"]))
	}
	details, detailsErr := s.details(ctx, token, ids)
	if detailsErr != nil {
		s.opts.Log("家庭成员资料暂未取全 │ SteamID=%s", sid)
	}
	countries, countryErr := s.countries(ctx, ids)
	if countryErr != nil {
		s.opts.Log("家庭成员国家暂未取全 │ SteamID=%s", sid)
	}
	var wg sync.WaitGroup
	previews := make([]map[string]any, len(group.Members))
	for i, m := range group.Members {
		d := details[str(m["steamid"])]
		m["personaName"] = str(d["persona_name"])
		m["avatarUrl"] = account.NormalizeAvatar(str(d["avatar_url"]))
		m["loccountrycode"] = optional(countries[str(m["steamid"])])
		if s.opts.Profile != nil && (memberName(m) == "" || avatarMissing(memberAvatar(m))) {
			wg.Add(1)
			go func() {
				defer wg.Done()
				p, e := s.opts.Profile(ctx, str(m["steamid"]))
				if e == nil {
					previews[i] = p
				}
			}()
		}
	}
	wg.Wait()
	if ctx.Err() != nil {
		return nil, ctx.Err()
	}
	for i, m := range group.Members {
		p := previews[i]
		if memberName(m) == "" {
			m["personaName"] = str(p["persona_name"])
		}
		if avatarMissing(memberAvatar(m)) {
			m["avatarUrl"] = account.NormalizeAvatar(str(p["avatar_url"]))
		}
	}
	tracked, added, e := s.saveGroup(ctx, sid, version, group)
	if e != nil {
		return nil, e
	}
	if s.opts.OnMembers != nil && len(added) > 0 {
		s.opts.OnMembers(added)
	}
	s.opts.Log("家庭组同步完成 │ SteamID=%s，家庭组=%s，成员=%d", sid, group.ID, len(tracked))
	v := map[string]any{"steamid": sid, "joined": group.ID != "", "memberCount": len(tracked)}
	if group.ID != "" {
		v["familyName"] = optional(group.Name)
	}
	return v, nil
}

func (s *Service) saveGroup(ctx context.Context, sid string, version bindingVersion, group remoteGroup) ([]string, []string, error) {
	var tracked, added []string
	err := s.gate.WithWrite(ctx, store.PriorityBackground, func(c *sql.Conn) error {
		if err := checkIdentity(ctx, c, sid, version); err != nil {
			return err
		}
		var err error
		tracked, added, err = s.saveGroupTx(ctx, c, sid, group)
		return err
	})
	return tracked, added, err
}

func (s *Service) saveGroupTx(ctx context.Context, c *sql.Conn, sid string, group remoteGroup) ([]string, []string, error) {
	tracked, added := []string{}, []string{}
	err := func() error {
		current, e := readGroup(ctx, c, sid)
		if e != nil {
			return e
		}
		if current == nil {
			current = &groupRow{SID: sid}
		}
		manual := []map[string]any{}
		excluded := map[string]map[string]any{}
		for _, m := range current.Members {
			if str(m["origin"]) == "manual" && !yes(m["excluded"]) {
				manual = append(manual, m)
			} else if yes(m["excluded"]) {
				excluded[str(m["steamid"])] = m
			}
		}
		steam := []map[string]any{}
		sourceMembers := []map[string]any{}
		for _, m := range group.Members {
			id := str(m["steamid"])
			if excluded[id] != nil {
				m["excluded"] = true
				delete(excluded, id)
			} else {
				sourceMembers = append(sourceMembers, m)
				tracked = append(tracked, id)
			}
			steam = append(steam, m)
		}
		members := append(steam, manual...)
		for _, m := range current.Members {
			if excluded[str(m["steamid"])] != nil {
				members = append(members, m)
				delete(excluded, str(m["steamid"]))
			}
		}
		if len(sourceMembers) > 0 {
			rows, e := c.QueryContext(ctx, `SELECT steamid FROM tracked_accounts WHERE steamid IN(SELECT value FROM json_each(?))`, packed(tracked))
			if e != nil {
				return e
			}
			existing := map[string]bool{}
			for rows.Next() {
				var id string
				if e = rows.Scan(&id); e != nil {
					rows.Close()
					return e
				}
				existing[id] = true
			}
			e = rows.Err()
			rows.Close()
			if e != nil {
				return e
			}
			for _, id := range tracked {
				if !existing[id] {
					added = append(added, id)
				}
			}
			_, e = c.ExecContext(ctx, `INSERT INTO tracked_accounts(steamid,label,persona_name,avatar_url,kinds_json,created_at)
 SELECT json_extract(value,'$.steamid'),NULLIF(json_extract(value,'$.personaName'),''),NULLIF(json_extract(value,'$.personaName'),''),NULLIF(json_extract(value,'$.avatarUrl'),''),'{"wishlist":true,"owned":true}',? FROM json_each(?) WHERE 1
 ON CONFLICT(steamid) DO UPDATE SET persona_name=COALESCE(NULLIF(excluded.persona_name,''),tracked_accounts.persona_name),avatar_url=COALESCE(NULLIF(excluded.avatar_url,''),tracked_accounts.avatar_url)`, s.stamp(), packed(sourceMembers))
			if e != nil {
				return e
			}
		}
		_, e = c.ExecContext(ctx, `INSERT INTO family_groups(steamid,family_groupid,family_name,members_json,fetch_ok,last_error,member_count,updated_at) VALUES(?,?,?,?,1,NULL,?,?) ON CONFLICT(steamid) DO UPDATE SET family_groupid=excluded.family_groupid,family_name=excluded.family_name,members_json=excluded.members_json,fetch_ok=1,last_error=NULL,play_json=CASE WHEN family_groups.family_groupid IS NOT excluded.family_groupid THEN NULL ELSE family_groups.play_json END,member_count=excluded.member_count,updated_at=excluded.updated_at`, sid, optional(group.ID), optional(group.Name), packed(members), len(sourceMembers), s.stamp())
		return e
	}()
	return tracked, added, err
}

type bindingVersion struct {
	credential string
	bound      sql.NullString
}

func readIdentity(ctx context.Context, q rowQuery, sid string) (bindingVersion, error) {
	var v bindingVersion
	e := q.QueryRowContext(ctx, `SELECT cookies,CAST(bound_at AS TEXT) FROM steam_accounts WHERE steam_id=?`, sid).Scan(&v.credential, &v.bound)
	if e == sql.ErrNoRows {
		return v, errIdentityChanged
	}
	return v, e
}
func checkIdentity(ctx context.Context, c *sql.Conn, sid string, version bindingVersion) error {
	current, e := readIdentity(ctx, c, sid)
	if e != nil {
		return e
	}
	if current != version {
		return errIdentityChanged
	}
	return nil
}
