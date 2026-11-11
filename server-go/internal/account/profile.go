package account

import (
	"context"
	"database/sql"
	"encoding/json"
	"encoding/xml"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"holdexar/server-go/internal/store"
)

func (c *SteamClient) Profile(ctx context.Context, sid string) (map[string]any, error) {
	client, e := c.client(ctx)
	if e != nil {
		return nil, e
	}
	copy := *client
	copy.Timeout = 10 * time.Second
	copy.Jar = nil
	client = &copy
	req, e := http.NewRequestWithContext(ctx, http.MethodGet, fmt.Sprintf(c.opts.ProfileURL, FriendCode(sid)), nil)
	if e != nil {
		return nil, e
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
	req.Header.Set("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
	resp, e := client.Do(req)
	if e != nil {
		return nil, walletError("network", "Steam 资料连接失败")
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, walletError("profile", fmt.Sprintf("Steam 资料暂不可用（HTTP %d）", resp.StatusCode))
	}
	var result map[string]any
	if json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&result) != nil || result == nil {
		return nil, walletError("profile", "Steam 资料响应无效")
	}
	return result, nil
}
func (c *SteamClient) ProfilePreview(ctx context.Context, sid string) (map[string]any, error) {
	p, _ := c.Profile(ctx, sid)
	if p == nil {
		p = map[string]any{}
	}
	missing := func(raw string) bool {
		tail := raw[strings.LastIndex(raw, "/")+1:]
		return raw == "" || strings.HasPrefix(tail, ".") || strings.HasPrefix(tail, "_")
	}
	avatar := textField(p["avatar_url"])
	if textField(p["persona_name"]) != "" && !missing(avatar) {
		p["avatar_url"] = NormalizeAvatar(avatar)
		return p, nil
	}
	client, e := c.client(ctx)
	if e != nil {
		return p, e
	}
	copy := *client
	copy.Jar = nil
	copy.Timeout = 10 * time.Second
	copy.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	req, e := http.NewRequestWithContext(ctx, http.MethodGet, fmt.Sprintf(c.opts.ProfileXMLURL, sid), nil)
	if e != nil {
		return p, e
	}
	req.Header.Set("User-Agent", "Mozilla/5.0")
	resp, e := copy.Do(req)
	if e != nil {
		return p, walletError("profile", "Steam 公开资料暂不可用")
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return p, walletError("profile", "Steam 公开资料暂不可用")
	}
	var profile struct {
		SID    string `xml:"steamID64"`
		Name   string `xml:"steamID"`
		Avatar string `xml:"avatarFull"`
		Medium string `xml:"avatarMedium"`
	}
	limit := &io.LimitedReader{R: resp.Body, N: (1 << 20) + 1}
	if xml.NewDecoder(limit).Decode(&profile) != nil || limit.N == 0 {
		return p, walletError("profile", "Steam 公开资料格式无效")
	}
	if profile.SID != "" && profile.SID != sid {
		return p, walletError("owner", "Steam 公开资料身份不一致")
	}
	if textField(p["persona_name"]) == "" {
		p["persona_name"] = strings.TrimSpace(profile.Name)
	}
	if missing(avatar) {
		avatar = profile.Avatar
		if avatar == "" {
			avatar = profile.Medium
		}
		p["avatar_url"] = NormalizeAvatar(strings.TrimSpace(avatar))
	}
	return p, nil
}
func textField(value any) string {
	if s, ok := value.(string); ok {
		return strings.TrimSpace(s)
	}
	return ""
}
func NormalizeAvatar(raw string) string {
	raw = strings.ReplaceAll(raw, "_medium", "_full")
	u, e := url.Parse(raw)
	if e != nil {
		return raw
	}
	host := strings.ToLower(u.Hostname())
	if strings.HasPrefix(host, "avatars.") {
		for _, suffix := range []string{"akamaized.net", "steamstatic.com", "queniuqe.com", "eccdnx.com"} {
			if strings.HasSuffix(host, suffix) {
				u.Scheme = "https"
				u.Host = "avatars.fastly.steamstatic.com"
				return u.String()
			}
		}
	}
	return raw
}
func (s *Service) syncProfile(ctx context.Context, row accountRow) {
	profile, e := s.opts.Steam.Profile(ctx, row.ID)
	if e != nil {
		return
	}
	name, avatar := textField(profile["persona_name"]), NormalizeAvatar(textField(profile["avatar_url"]))
	inGame := ""
	for _, key := range []string{"in_game", "in_nonsteam_game"} {
		if value, ok := profile[key].(map[string]any); ok {
			inGame = textField(value["name"])
			if inGame == "" {
				inGame = textField(value["game_name"])
			}
		} else {
			inGame = textField(profile[key])
		}
		if inGame != "" {
			break
		}
	}
	online, _ := profile["online"].(bool)
	e = s.gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		_, e := conn.ExecContext(ctx, `UPDATE steam_accounts SET persona_name=CASE WHEN persona_name='' THEN ? ELSE persona_name END,avatar_url=CASE WHEN ?!='' THEN ? ELSE avatar_url END,is_online=?,in_game=? WHERE steam_id=? AND cookies=? AND CAST(bound_at AS TEXT) IS ?`, name, avatar, avatar, online, profileGame(inGame), row.ID, row.Cookies, row.Bound)
		return e
	})
	if e != nil {
		s.opts.Log("账号资料本轮未保存 │ SteamID=%s", row.ID)
	}
}
