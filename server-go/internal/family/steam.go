package family

import (
	"context"
	"fmt"
	"net/url"

	"holdexar/server-go/internal/account"
)

type remoteGroup struct {
	ID, Name string
	Members  []map[string]any
}

func (s *Service) fetchGroup(ctx context.Context, token string) (remoteGroup, error) {
	var body struct {
		Response map[string]any `json:"response"`
	}
	if e := s.opts.Lists.GetJSON(ctx, s.opts.GroupURL, url.Values{"access_token": {token}, "include_family_group_response": {"true"}}, &body); e != nil {
		return remoteGroup{}, e
	}
	if body.Response == nil {
		return remoteGroup{}, fmt.Errorf("Steam 家庭组响应无效")
	}
	id := str(body.Response["family_groupid"])
	if id == "" || id == "0" {
		return remoteGroup{Members: []map[string]any{}}, nil
	}
	raw, ok := body.Response["family_group"].(map[string]any)
	if !ok {
		return remoteGroup{}, fmt.Errorf("Steam 家庭组详情缺失")
	}
	g := remoteGroup{ID: id, Name: str(raw["name"]), Members: []map[string]any{}}
	members, ok := raw["members"].([]any)
	if raw["members"] != nil && !ok {
		return remoteGroup{}, fmt.Errorf("Steam 家庭成员响应无效")
	}
	seen := map[string]bool{}
	for _, v := range members {
		m, ok := v.(map[string]any)
		if !ok {
			return remoteGroup{}, fmt.Errorf("Steam 家庭成员响应无效")
		}
		sid := str(m["steamid"])
		if !validSID(sid) {
			return remoteGroup{}, fmt.Errorf("Steam 家庭成员身份无效")
		}
		if seen[sid] {
			continue
		}
		seen[sid] = true
		g.Members = append(g.Members, map[string]any{"steamid": sid, "role": str(m["role"]), "origin": "steam", "personaName": "", "avatarUrl": "", "loccountrycode": nil})
	}
	return g, nil
}
func (s *Service) details(ctx context.Context, token string, ids []string) (map[string]map[string]any, error) {
	out := map[string]map[string]any{}
	if len(ids) == 0 {
		return out, nil
	}
	q := url.Values{"access_token": {token}}
	for i, id := range ids {
		q.Set(fmt.Sprintf("steamids[%d]", i), id)
	}
	var body struct {
		Response *struct {
			Accounts []struct {
				Public map[string]any `json:"public_data"`
			} `json:"accounts"`
		} `json:"response"`
	}
	if e := s.opts.Lists.GetJSON(ctx, s.opts.LinkURL, q, &body); e != nil {
		return nil, e
	}
	if body.Response == nil {
		return nil, fmt.Errorf("Steam 家庭成员资料响应无效")
	}
	for _, a := range body.Response.Accounts {
		sid := str(a.Public["steamid"])
		if sid != "" {
			out[sid] = map[string]any{"persona_name": str(a.Public["persona_name"]), "avatar_url": account.NormalizeAvatar(str(a.Public["avatar"]))}
		}
	}
	return out, nil
}
func (s *Service) countries(ctx context.Context, ids []string) (map[string]string, error) {
	out := map[string]string{}
	if len(ids) == 0 || s.opts.APIKey == nil {
		return out, nil
	}
	key, e := s.opts.APIKey(ctx)
	if e != nil {
		return nil, e
	}
	if key == "" {
		return out, nil
	}
	for start := 0; start < len(ids); start += 100 {
		end := min(start+100, len(ids))
		players, e := s.opts.Lists.PlayerSummaries(ctx, s.opts.SummariesURL, ids[start:end], key)
		if e != nil {
			return nil, e
		}
		for id, p := range players {
			if p.Country != "" {
				out[id] = p.Country
			}
		}
	}
	return out, nil
}
