package wishlist

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

const steamBase int64 = 76561197960265728

type Wish struct {
	AppID   int64 `json:"appid"`
	AddedAt int64 `json:"added_at"`
}
type OwnedGame struct {
	AppID      int64  `json:"appid"`
	Name       string `json:"name"`
	Minutes    int64  `json:"playtime_forever"`
	Minutes2w  int64  `json:"playtime_2weeks"`
	LastPlayed int64  `json:"rtime_last_played"`
}
type SteamOptions struct {
	HTTPClient                        *http.Client
	WishlistURL, OwnedURL, ResolveURL string
	Proxy                             func(context.Context) (string, error)
	Token                             func(context.Context, string) (string, error)
	APIKey                            func(context.Context) (string, error)
}
type Steam struct {
	opts    SteamOptions
	mu      sync.Mutex
	clients map[string]*http.Client
}

func NewSteam(opts SteamOptions) *Steam {
	if opts.WishlistURL == "" {
		opts.WishlistURL = "https://api.steampowered.com/IWishlistService/GetWishlist/v1/"
	}
	if opts.OwnedURL == "" {
		opts.OwnedURL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
	}
	if opts.ResolveURL == "" {
		opts.ResolveURL = "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/"
	}
	return &Steam{opts: opts, clients: map[string]*http.Client{}}
}
func (s *Steam) client(ctx context.Context) (*http.Client, error) {
	if s.opts.HTTPClient != nil {
		client := *s.opts.HTTPClient
		client.Jar = nil
		return &client, nil
	}
	proxy := ""
	if s.opts.Proxy != nil {
		var e error
		if proxy, e = s.opts.Proxy(ctx); e != nil {
			return nil, fmt.Errorf("Steam 出口未就绪")
		}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if client := s.clients[proxy]; client != nil {
		return client, nil
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	if proxy != "" {
		address, e := url.Parse(proxy)
		if e != nil || address.Host == "" {
			return nil, fmt.Errorf("Steam 出口配置无效")
		}
		transport.Proxy = http.ProxyURL(address)
	}
	client := &http.Client{Transport: transport}
	s.clients[proxy] = client
	return client, nil
}
func (s *Steam) Close() {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, client := range s.clients {
		client.CloseIdleConnections()
	}
}
func (s *Steam) GetJSON(ctx context.Context, endpoint string, query url.Values, target any) error {
	client, e := s.client(ctx)
	if e != nil {
		return e
	}
	copy := *client
	copy.Timeout = 20 * time.Second
	copy.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	address, e := url.Parse(endpoint)
	if e != nil {
		return fmt.Errorf("Steam 接口地址无效")
	}
	address.RawQuery = query.Encode()
	req, e := http.NewRequestWithContext(ctx, http.MethodGet, address.String(), nil)
	if e != nil {
		return fmt.Errorf("Steam 请求参数无效")
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
	resp, e := copy.Do(req)
	if e != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return fmt.Errorf("Steam 请求连接失败")
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("Steam API 返回 HTTP %d", resp.StatusCode)
	}
	limited := &io.LimitedReader{R: resp.Body, N: (64 << 20) + 1}
	decoder := json.NewDecoder(limited)
	decoder.UseNumber()
	if decoder.Decode(target) != nil {
		return fmt.Errorf("Steam 返回的清单格式无效")
	}
	var extra any
	if decoder.Decode(&extra) != io.EOF || limited.N == 0 {
		return fmt.Errorf("Steam 返回的清单格式无效")
	}
	return nil
}

type PlayerSummary struct {
	State   int
	InGame  string
	Country string
}

func (s *Steam) PlayerSummaries(ctx context.Context, endpoint string, ids []string, key string) (map[string]PlayerSummary, error) {
	if endpoint == "" {
		endpoint = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
	}
	var body struct {
		Response *struct {
			Players []struct {
				ID      string          `json:"steamid"`
				State   json.RawMessage `json:"personastate"`
				InGame  string          `json:"gameextrainfo"`
				Country string          `json:"loccountrycode"`
			} `json:"players"`
		} `json:"response"`
	}
	if err := s.GetJSON(ctx, endpoint, url.Values{"key": {key}, "steamids": {strings.Join(ids, ",")}}, &body); err != nil {
		return nil, err
	}
	if body.Response == nil {
		return nil, fmt.Errorf("Steam 账号资料响应无效")
	}
	requested := make(map[string]bool, len(ids))
	for _, id := range ids {
		requested[id] = true
	}
	out := make(map[string]PlayerSummary, len(body.Response.Players))
	for _, p := range body.Response.Players {
		if requested[p.ID] {
			state, _ := strconv.Atoi(strings.Trim(string(p.State), `"`))
			out[p.ID] = PlayerSummary{State: state, InGame: strings.TrimSpace(p.InGame), Country: strings.ToUpper(strings.TrimSpace(p.Country))}
		}
	}
	return out, nil
}

func (s *Steam) Wishlist(ctx context.Context, sid string) ([]Wish, error) {
	var body struct {
		Response *struct {
			Items []Wish `json:"items"`
		} `json:"response"`
	}
	if e := s.GetJSON(ctx, s.opts.WishlistURL, url.Values{"steamid": {sid}}, &body); e != nil {
		return nil, e
	}
	if body.Response == nil {
		return nil, fmt.Errorf("Steam 返回的愿望单格式无效")
	}
	items := []Wish{}
	seen := map[int64]bool{}
	for _, item := range body.Response.Items {
		if item.AppID > 0 && !seen[item.AppID] {
			seen[item.AppID] = true
			items = append(items, item)
		}
	}
	return items, nil
}
func (s *Steam) owned(ctx context.Context, sid, credential, channel string) ([]OwnedGame, error) {
	query := url.Values{"steamid": {sid}, "include_appinfo": {"1"}, "include_played_free_games": {"1"}, channel: {credential}}
	var body struct {
		Response *struct {
			Games []OwnedGame `json:"games"`
		} `json:"response"`
	}
	if e := s.GetJSON(ctx, s.opts.OwnedURL, query, &body); e != nil {
		return nil, e
	}
	if body.Response == nil {
		return nil, fmt.Errorf("Steam 返回的已购清单格式无效")
	}
	items := []OwnedGame{}
	seen := map[int64]bool{}
	for _, item := range body.Response.Games {
		if item.AppID <= 0 {
			return nil, fmt.Errorf("Steam 已购清单包含无效游戏")
		}
		if !seen[item.AppID] {
			seen[item.AppID] = true
			items = append(items, item)
		}
	}
	return items, nil
}

func (s *Steam) OwnedWithToken(ctx context.Context, sid, token string) ([]OwnedGame, error) {
	return s.owned(ctx, sid, token, "access_token")
}
func (s *Steam) Owned(ctx context.Context, sid string) ([]OwnedGame, string, error) {
	var last error
	if s.opts.Token != nil {
		token, e := s.opts.Token(ctx, sid)
		if e != nil {
			last = fmt.Errorf("无法读取该账号的可用登录凭据")
		} else if token != "" {
			items, e := s.owned(ctx, sid, token, "access_token")
			if e == nil {
				return items, "jwt", nil
			}
			last = e
		}
	}
	if ctx.Err() != nil {
		return nil, "", ctx.Err()
	}
	if s.opts.APIKey != nil {
		key, e := s.opts.APIKey(ctx)
		if e != nil {
			last = fmt.Errorf("无法读取 Web API Key")
		} else if key != "" {
			items, e := s.owned(ctx, sid, key, "key")
			if e == nil {
				return items, "key", nil
			}
			last = e
		}
	}
	if last != nil {
		return nil, "", fmt.Errorf("已购拉取失败：%v", last)
	}
	return nil, "", fmt.Errorf("已购拉取不可用：未绑定可用的 Steam Cookie，且未配置 Web API Key")
}

var vanityName = regexp.MustCompile(`^[a-zA-Z0-9_-]+$`)

func steamID(text string) bool {
	if len(text) != 17 {
		return false
	}
	id, e := strconv.ParseInt(text, 10, 64)
	return e == nil && id >= steamBase
}
func (s *Steam) Resolve(ctx context.Context, raw string) (string, error) {
	raw = strings.TrimSpace(raw)
	if steamID(raw) {
		return raw, nil
	}
	if strings.HasPrefix(raw, "steamcommunity.com/") || strings.HasPrefix(raw, "www.steamcommunity.com/") {
		raw = "https://" + raw
	}
	vanity := raw
	if strings.Contains(raw, "://") || strings.HasPrefix(raw, "//") {
		if strings.HasPrefix(raw, "//") {
			raw = "https:" + raw
		}
		u, e := url.Parse(raw)
		if e != nil || u.User != nil || u.Scheme != "https" && u.Scheme != "http" || !strings.EqualFold(u.Hostname(), "steamcommunity.com") && !strings.EqualFold(u.Hostname(), "www.steamcommunity.com") {
			return "", fmt.Errorf("无法识别的 Steam 个人资料链接")
		}
		parts := strings.Split(strings.Trim(u.Path, "/"), "/")
		if len(parts) != 2 {
			return "", fmt.Errorf("无法识别的 Steam 个人资料链接")
		}
		if parts[0] == "profiles" && steamID(parts[1]) {
			return parts[1], nil
		}
		if parts[0] != "id" {
			return "", fmt.Errorf("无法识别的 Steam 个人资料链接")
		}
		vanity = parts[1]
	} else if number, e := strconv.ParseInt(raw, 10, 64); e == nil {
		if len(raw) >= 4 && len(raw) <= 10 && !strings.HasPrefix(raw, "7656") && number >= 0 {
			return strconv.FormatInt(number+steamBase, 10), nil
		}
		return "", fmt.Errorf("无法识别的 Steam 标识符")
	}
	if !vanityName.MatchString(vanity) {
		return "", fmt.Errorf("无法识别的 Steam 标识符")
	}
	key := ""
	if s.opts.APIKey != nil {
		var e error
		if key, e = s.opts.APIKey(ctx); e != nil {
			return "", fmt.Errorf("无法读取 Web API Key")
		}
	}
	if key == "" {
		return "", fmt.Errorf("支持好友码、SteamID64 或个人资料链接；自定义链接需在设置「账户」页配置 API Key")
	}
	var body struct {
		Response *struct {
			Success int    `json:"success"`
			SteamID string `json:"steamid"`
		} `json:"response"`
	}
	if e := s.GetJSON(ctx, s.opts.ResolveURL, url.Values{"key": {key}, "vanityurl": {vanity}}, &body); e != nil {
		return "", e
	}
	if body.Response == nil || body.Response.Success != 1 || !steamID(body.Response.SteamID) {
		return "", fmt.Errorf("无法解析自定义 Steam 链接")
	}
	return body.Response.SteamID, nil
}
