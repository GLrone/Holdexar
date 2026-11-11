package family

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"sort"
	"strings"
	"sync"

	"holdexar/server-go/internal/account"
	"holdexar/server-go/internal/store"
	"holdexar/server-go/internal/wishlist"
)

type sharedApp struct {
	ID            int64         `json:"appid"`
	Name          string        `json:"name"`
	Owners        []json.Number `json:"owner_steamids"`
	Acquired      int64         `json:"rt_time_acquired"`
	Presence      int           `json:"presence_count"`
	ExcludeReason int           `json:"exclude_reason"`
}

type memberLibrary struct {
	games   []wishlist.OwnedGame
	bound   bool
	version bindingVersion
	err     error
}

func (s *Service) sharedLibrary(ctx context.Context, groupID, token string) ([]sharedApp, error) {
	var response struct {
		Response *struct {
			Apps []sharedApp `json:"apps"`
		} `json:"response"`
	}
	err := s.opts.Lists.GetJSON(ctx, s.opts.SharedURL, url.Values{"access_token": {token}, "family_groupid": {groupID}, "include_own": {"true"}, "include_excluded": {"true"}}, &response)
	if err != nil {
		return nil, err
	}
	if response.Response == nil {
		return nil, fmt.Errorf("Steam 家庭库响应无效")
	}
	for _, app := range response.Response.Apps {
		if app.ID <= 0 {
			return nil, fmt.Errorf("Steam 家庭库包含无效游戏")
		}
	}
	return response.Response.Apps, nil
}

func excludedMembers(g *groupRow) map[string]bool {
	excluded := map[string]bool{}
	if g != nil {
		for _, m := range g.Members {
			if yes(m["excluded"]) {
				excluded[str(m["steamid"])] = true
			}
		}
	}
	return excluded
}

func steamMemberIDs(members []map[string]any, excluded map[string]bool) string {
	ids := []string{}
	for _, m := range members {
		id := str(m["steamid"])
		if id != "" && str(m["origin"]) != "manual" && !excluded[id] {
			ids = append(ids, id)
		}
	}
	sort.Strings(ids)
	return packed(ids)
}

func (s *Service) refreshLibrary(ctx context.Context, sid string) (*libraryPayload, libraryState, error) {
	var state libraryState
	token, version, err := s.stableToken(ctx, sid)
	if err != nil || token == "" {
		return nil, state, &InputError{"该账号无可用登录 Cookie，请先绑定"}
	}
	before, saved, err := loadLibraryState(ctx, s.db, sid)
	if err != nil {
		return nil, state, err
	}
	if !before.bound || before.binding != version {
		return nil, state, errIdentityChanged
	}
	group, err := s.fetchGroup(ctx, token)
	if err != nil {
		return nil, state, err
	}
	if group.ID == "" {
		if err := s.gate.WithWrite(ctx, store.PriorityBackground, func(c *sql.Conn) error {
			current, _, err := loadLibraryState(ctx, c, sid)
			if err != nil {
				return err
			}
			if current != before {
				return errIdentityChanged
			}
			_, _, err = s.saveGroupTx(ctx, c, sid, group)
			return err
		}); err != nil {
			return nil, state, err
		}
		return nil, state, &InputError{"当前账号未加入家庭组，无法拉取家庭库"}
	}
	shared, err := s.sharedLibrary(ctx, group.ID, token)
	if err != nil {
		return nil, state, err
	}
	play := map[string][]libraryPlay{}
	known := map[string]map[string]any{}
	if saved != nil && saved.Group.String == group.ID {
		play, err = savedPlay(saved)
		if err != nil {
			return nil, state, err
		}
		for _, m := range saved.Members {
			if !yes(m["excluded"]) {
				known[str(m["steamid"])] = m
			}
		}
	}
	excluded := excludedMembers(saved)
	previousMembers := "[]"
	if saved != nil {
		previousMembers = steamMemberIDs(saved.Members, excluded)
	}
	fetchedMembers := steamMemberIDs(group.Members, excluded)
	members := []map[string]any{}
	missing := []string{}
	for _, m := range group.Members {
		id := str(m["steamid"])
		if excluded[id] {
			continue
		}
		m["personaName"], m["avatarUrl"] = memberName(known[id]), memberAvatar(known[id])
		members = append(members, m)
		if memberName(m) == "" || avatarMissing(memberAvatar(m)) {
			missing = append(missing, id)
		}
	}
	profiles, _ := s.details(ctx, token, missing)
	accounts, err := s.bound(ctx)
	if err != nil {
		return nil, state, err
	}
	bound := map[string]bool{}
	for _, a := range accounts {
		bound[a.SID] = true
	}
	results := make([]memberLibrary, len(members))
	var workers sync.WaitGroup
	for i, m := range members {
		workers.Go(func() {
			id := str(m["steamid"])
			fillLibraryProfile(m, profiles[id])
			if s.opts.Profile != nil && (memberName(m) == "" || avatarMissing(memberAvatar(m))) {
				if p, err := s.opts.Profile(ctx, id); err == nil {
					fillLibraryProfile(m, p)
				}
			}
			result := &results[i]
			result.bound = bound[id]
			memberToken := token
			if id == sid {
				result.bound, result.version = true, version
			} else if result.bound {
				memberToken, result.version, result.err = s.stableToken(ctx, id)
			}
			if result.err == nil && memberToken != "" {
				result.games, result.err = s.opts.Lists.OwnedWithToken(ctx, id, memberToken)
			} else if result.err == nil {
				result.err = fmt.Errorf("该成员登录凭据不可用")
			}
		})
	}
	workers.Wait()
	if err = ctx.Err(); err != nil {
		return nil, state, err
	}
	data, names := s.mergeLibrary(group, shared, members, results, play)
	ids := make([]int64, 0, len(data.Games))
	for _, game := range data.Games {
		ids = append(ids, game.AppID)
	}
	meta, err := s.gameMetadata(ctx, ids)
	if err != nil {
		return nil, state, err
	}
	for i := range data.Games {
		game := &data.Games[i]
		game.gameInfo = gameWithInfo(game.AppID, meta[game.AppID], str(game.Name))
	}
	var added []string
	err = s.gate.WithWrite(ctx, store.PriorityBackground, func(c *sql.Conn) error {
		current, row, err := loadLibraryState(ctx, c, sid)
		if err != nil {
			return err
		}
		if !current.bound || current.binding != version ||
			(current.group != before.group && current.group != group.ID) ||
			packed(excludedMembers(row)) != packed(excluded) {
			return errIdentityChanged
		}
		currentMembers := "[]"
		if row != nil {
			currentMembers = steamMemberIDs(row.Members, excluded)
		}
		if currentMembers != fetchedMembers && (current.group != before.group || currentMembers != previousMembers) {
			return errIdentityChanged
		}
		for i, member := range members {
			r := results[i]
			if r.err != nil {
				continue
			}
			id := str(member["steamid"])
			if r.bound {
				if err := checkIdentity(ctx, c, id, r.version); err != nil {
					return err
				}
			} else if _, err := readIdentity(ctx, c, id); !errors.Is(err, errIdentityChanged) {
				if err != nil {
					return err
				}
				return errIdentityChanged
			}
		}
		if current.group != group.ID || currentMembers != fetchedMembers {
			_, added, err = s.saveGroupTx(ctx, c, sid, group)
			if err != nil {
				return err
			}
		}
		if err := seedLibraryNames(ctx, c, names, s.stamp()); err != nil {
			return err
		}
		if err := s.saveLibrary(ctx, c, sid, data); err != nil {
			return err
		}
		state, _, err = loadLibraryState(ctx, c, sid)
		return err
	})
	if err != nil {
		return nil, state, err
	}
	if s.opts.OnMembers != nil && len(added) > 0 {
		s.opts.OnMembers(added)
	}
	s.opts.Log("家庭库快照已保存 │ SteamID=%s，家庭组=%s，游戏=%d", sid, group.ID, len(data.Games))
	return data, state, nil
}

func fillLibraryProfile(member, profile map[string]any) {
	if memberName(member) == "" {
		member["personaName"] = str(profile["persona_name"])
	}
	if avatarMissing(memberAvatar(member)) {
		member["avatarUrl"] = account.NormalizeAvatar(str(profile["avatar_url"]))
	}
}

func (s *Service) mergeLibrary(group remoteGroup, shared []sharedApp, members []map[string]any, results []memberLibrary, previous map[string][]libraryPlay) (*libraryPayload, map[int64]string) {
	data := &libraryPayload{GroupID: group.ID, Name: optional(group.Name), Members: []libraryMember{}, Games: []libraryGame{}, Play: map[string][]libraryPlay{}}
	owned := map[int64][]string{}
	names := map[int64]string{}
	for i, m := range members {
		id := str(m["steamid"])
		res := results[i]
		data.Members = append(data.Members, libraryMember{SID: id, Role: str(m["role"]), Name: memberName(m), Avatar: memberAvatar(m), Owned: len(res.games)})
		if res.err != nil {
			s.opts.Log("成员已购暂未取全，保留已有游玩记录 │ SteamID=%s", id)
			if old := previous[id]; old != nil {
				data.Play[id] = old
			} else {
				data.Play[id] = []libraryPlay{}
			}
			continue
		}
		play := []libraryPlay{}
		for _, game := range res.games {
			owned[game.AppID] = append(owned[game.AppID], id)
			if names[game.AppID] == "" && strings.TrimSpace(game.Name) != "" {
				names[game.AppID] = strings.TrimSpace(game.Name)
			}
			if game.Minutes > 0 || game.Minutes2w > 0 {
				play = append(play, libraryPlay{AppID: game.AppID, Minutes: game.Minutes, Minutes2w: game.Minutes2w, Last: game.LastPlayed})
			}
		}
		sort.SliceStable(play, func(i, j int) bool {
			if play[i].Minutes2w != play[j].Minutes2w {
				return play[i].Minutes2w > play[j].Minutes2w
			}
			return play[i].Minutes > play[j].Minutes
		})
		data.Play[id] = play
	}
	byApp := map[int64]sharedApp{}
	for _, app := range shared {
		byApp[app.ID] = app
	}
	data.SharedCount = len(byApp)
	ids := make([]int64, 0, len(byApp)+len(owned))
	for id := range byApp {
		ids = append(ids, id)
	}
	for id := range owned {
		if _, ok := byApp[id]; !ok {
			ids = append(ids, id)
		}
	}
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
	for _, id := range ids {
		app, isShared := byApp[id]
		owners := owned[id]
		if len(app.Owners) > 0 {
			owners = make([]string, 0, len(app.Owners))
			for _, owner := range app.Owners {
				owners = append(owners, owner.String())
			}
		}
		if owners == nil {
			owners = []string{}
		}
		name := names[id]
		if name == "" {
			name = strings.TrimSpace(app.Name)
		}
		game := libraryGame{gameInfo: gameWithInfo(id, gameInfo{}, name), Owners: owners, OwnerCount: len(owned[id]), Presence: app.Presence, Excluded: app.ExcludeReason != 0, Shared: isShared, Acquired: app.Acquired}
		if !isShared {
			game.Presence = len(owners)
		}
		if len(owners) > 0 {
			game.Buyer = owners[len(owners)-1]
		}
		data.Games = append(data.Games, game)
	}
	applyPlay(data.Games, data.Play)
	return data, names
}

func (s *Service) saveLibrary(ctx context.Context, c *sql.Conn, sid string, data *libraryPayload) error {
	_, err := c.ExecContext(ctx, `INSERT INTO family_library_snapshots(appid,family_groupid,name,owners_json,time_acquired,presence,excluded,updated_at)
SELECT json_extract(value,'$.appid'),?,json_extract(value,'$.name'),json_extract(value,'$.owners'),json_extract(value,'$.timeAcquired'),json_extract(value,'$.presence'),json_extract(value,'$.excluded'),?
FROM json_each(?) WHERE 1
ON CONFLICT(appid,family_groupid) DO UPDATE SET name=COALESCE(excluded.name,family_library_snapshots.name),owners_json=excluded.owners_json,time_acquired=excluded.time_acquired,presence=excluded.presence,excluded=excluded.excluded,updated_at=excluded.updated_at`, data.GroupID, s.stamp(), packed(data.Games))
	if err != nil {
		return err
	}
	_, err = c.ExecContext(ctx, `UPDATE family_groups SET play_json=? WHERE steamid=? AND family_groupid=?`, packed(data.Play), sid, data.GroupID)
	return err
}
