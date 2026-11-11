package achievements

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/netenv"
)

const (
	steamAPI          = "https://api.steampowered.com"
	steamCommunity    = "https://steamcommunity.com"
	steamLanguage     = "schinese"
	steamProgressSize = 100
	steamBodyLimit    = 32 << 20
)

var (
	errSteamCredentials = errors.New("Steam 成就同步需要有效的登录凭据或 Web API Key")
	errSteamNetwork     = errors.New("Steam 请求连接失败")
	errSteamResponse    = errors.New("Steam 返回的成就数据格式无效")
	errSteamUnavailable = errors.New("Steam 未返回可读取的成就数据")
)

type Credentials struct{ Key, Token string }

type OwnedGame struct {
	AppID      int64  `json:"appid"`
	Name       string `json:"name"`
	Playtime   int64  `json:"playtime_forever"`
	LastPlayed int64  `json:"rtime_last_played"`
}

type Progress struct{ Total, Unlocked int }

type Definition struct {
	ImageName, APIName, Name, Description, IconURL, GrayIconURL string
	Percent                                                     *float64
	Order                                                       int
}

type State struct {
	Achieved    bool
	UnlockTime  int64
	GrayIconURL string
}

type SteamOptions struct {
	HTTPClient *http.Client
	Proxy      func(context.Context) (string, error)
}

type Steam struct {
	client    http.Client
	transport *http.Transport
}

func NewSteam(opts SteamOptions) *Steam {
	if opts.HTTPClient != nil {
		client := *opts.HTTPClient
		client.Jar = nil
		return &Steam{client: client}
	}
	proxy := opts.Proxy
	if proxy == nil {
		proxy = netenv.ResolveSystemProxy
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = func(req *http.Request) (*url.URL, error) {
		address, err := proxy(req.Context())
		if err != nil || address == "" {
			return nil, err
		}
		return url.Parse(address)
	}
	return &Steam{client: http.Client{Transport: transport}, transport: transport}
}

func (s *Steam) Close() {
	if s.transport != nil {
		s.transport.CloseIdleConnections()
	}
}

type steamStatus int

func (status steamStatus) Error() string { return fmt.Sprintf("Steam 返回 HTTP %d", status) }

func (s *Steam) request(ctx context.Context, method, endpoint string, params url.Values, public bool) ([]byte, error) {
	client := s.client
	if client.Timeout == 0 {
		client.Timeout = 30 * time.Second
	}
	client.CheckRedirect = func(req *http.Request, via []*http.Request) error {
		if public && len(via) < 10 && req.URL.Scheme == "https" && req.URL.Host == "steamcommunity.com" && req.URL.User == nil {
			query := req.URL.Query()
			query.Set("l", steamLanguage)
			req.URL.RawQuery = query.Encode()
			return nil
		}
		return http.ErrUseLastResponse
	}
	var form io.Reader
	if method == http.MethodPost {
		form = strings.NewReader(params.Encode())
	} else if len(params) > 0 {
		endpoint += "?" + params.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, method, endpoint, form)
	if err != nil {
		return nil, errSteamResponse
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125")
	if form != nil {
		req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	}
	if public {
		req.Header.Set("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
	}
	resp, err := client.Do(req)
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		var tlsErr *tls.CertificateVerificationError
		if errors.As(err, &tlsErr) {
			return nil, errors.New("Steam 连接证书校验失败")
		}
		return nil, errSteamNetwork
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, steamStatus(resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, steamBodyLimit+1))
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		return nil, errSteamNetwork
	}
	if len(body) > steamBodyLimit {
		return nil, errSteamResponse
	}
	return body, nil
}

func (s *Steam) retryRequest(ctx context.Context, method, endpoint string, params url.Values, public bool) ([]byte, error) {
	for attempt := 1; ; attempt++ {
		body, err := s.request(ctx, method, endpoint, params, public)
		status, _ := err.(steamStatus)
		if attempt == 3 || err != errSteamNetwork && !(public && (status == 429 || status >= 500)) {
			return body, err
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(time.Duration(2*attempt) * time.Second):
		}
	}
}

func (s *Steam) webAPI(ctx context.Context, method, endpoint string, params url.Values, creds Credentials, optional bool, decode func([]byte) error) error {
	last := errSteamCredentials
	for _, credential := range []struct{ field, value string }{{"key", creds.Key}, {"access_token", creds.Token}} {
		if credential.value == "" {
			continue
		}
		params.Set(credential.field, credential.value)
		body, err := s.retryRequest(ctx, method, steamAPI+endpoint, params, false)
		params.Del(credential.field)
		if err == nil {
			err = decode(body)
		}
		if err == nil {
			return nil
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		last = err
		if !optional && err != steamStatus(401) && err != steamStatus(403) && err != errSteamResponse && err != errSteamUnavailable {
			break
		}
	}
	return last
}

func (s *Steam) Owned(ctx context.Context, sid string, creds Credentials) ([]OwnedGame, error) {
	var games []OwnedGame
	params := url.Values{"steamid": {sid}, "include_appinfo": {"1"}, "include_played_free_games": {"1"}}
	err := s.webAPI(ctx, http.MethodGet, "/IPlayerService/GetOwnedGames/v1/", params, creds, false, func(data []byte) error {
		var body struct {
			Response *struct {
				Count *int        `json:"game_count"`
				Games []OwnedGame `json:"games"`
			} `json:"response"`
		}
		if json.Unmarshal(data, &body) != nil || body.Response == nil {
			return errSteamResponse
		}
		library := body.Response
		if library.Count == nil && library.Games == nil {
			return errSteamUnavailable
		}
		if library.Count != nil && *library.Count != len(library.Games) {
			return errSteamResponse
		}
		seen := make(map[int64]bool, len(library.Games))
		valid := make([]OwnedGame, 0, len(library.Games))
		for _, game := range library.Games {
			if game.AppID <= 0 || game.Playtime < 0 || game.LastPlayed < 0 {
				return errSteamResponse
			}
			if !seen[game.AppID] {
				seen[game.AppID] = true
				valid = append(valid, game)
			}
		}
		games = valid
		return nil
	})
	return games, err
}

func (s *Steam) Progress(ctx context.Context, sid string, creds Credentials, ids []int64) (map[int64]Progress, error) {
	out := make(map[int64]Progress, len(ids))
	for start := 0; start < len(ids); start += steamProgressSize {
		batch := ids[start:min(start+steamProgressSize, len(ids))]
		params := url.Values{"steamid": {sid}}
		requested := make(map[int64]bool, len(batch))
		for index, appID := range batch {
			params.Set(fmt.Sprintf("appids[%d]", index), strconv.FormatInt(appID, 10))
			requested[appID] = true
		}
		err := s.webAPI(ctx, http.MethodPost, "/IPlayerService/GetAchievementsProgress/v1/", params, creds, false, func(data []byte) error {
			var body struct {
				Response *struct {
					Rows []json.RawMessage `json:"achievement_progress"`
				} `json:"response"`
			}
			if json.Unmarshal(data, &body) != nil || body.Response == nil || body.Response.Rows == nil {
				return errSteamResponse
			}
			for _, row := range body.Response.Rows {
				var item struct {
					AppID    json.Number `json:"appid"`
					Total    json.Number `json:"total"`
					Unlocked json.Number `json:"unlocked"`
				}
				if json.Unmarshal(row, &item) != nil {
					continue
				}
				appID, appErr := item.AppID.Int64()
				total, totalErr := strconv.Atoi(item.Total.String())
				var unlocked int
				var unlockErr error
				if item.Unlocked != "" {
					unlocked, unlockErr = strconv.Atoi(item.Unlocked.String())
				}
				if appErr == nil && totalErr == nil && unlockErr == nil && requested[appID] && total >= 0 && unlocked >= 0 && unlocked <= total {
					out[appID] = Progress{Total: total, Unlocked: unlocked}
				}
			}
			return nil
		})
		if err != nil {
			return out, err
		}
	}
	return out, nil
}

func (s *Steam) PlayerStates(ctx context.Context, sid string, appID int64, creds Credentials) (map[string]State, error) {
	var states map[string]State
	params := url.Values{"steamid": {sid}, "appid": {strconv.FormatInt(appID, 10)}, "l": {steamLanguage}}
	err := s.webAPI(ctx, http.MethodGet, "/ISteamUserStats/GetPlayerAchievements/v1/", params, Credentials{Key: creds.Key}, true, func(data []byte) error {
		var body struct {
			Player *struct {
				Owner   string `json:"steamID"`
				Success *bool  `json:"success"`
				Rows    []struct {
					APIName  string      `json:"apiname"`
					Achieved json.Number `json:"achieved"`
					Time     json.Number `json:"unlocktime"`
				} `json:"achievements"`
			} `json:"playerstats"`
		}
		if json.Unmarshal(data, &body) != nil || body.Player == nil {
			return errSteamResponse
		}
		player := body.Player
		if player.Success != nil && !*player.Success || player.Owner != "" && player.Owner != sid || len(player.Rows) == 0 {
			return errSteamUnavailable
		}
		valid := make(map[string]State, len(player.Rows))
		for _, row := range player.Rows {
			achieved, err := row.Achieved.Int64()
			if row.APIName == "" || err != nil || achieved < 0 || achieved > 1 {
				return errSteamResponse
			}
			var unlockTime int64
			if row.Time != "" {
				unlockTime, err = row.Time.Int64()
				if err != nil || unlockTime < 0 {
					return errSteamResponse
				}
			}
			valid[row.APIName] = State{Achieved: achieved == 1, UnlockTime: unlockTime}
		}
		states = valid
		return nil
	})
	return states, err
}

func MatchAPIStates(raw map[string]State, defs []Definition) map[string]State {
	out := make(map[string]State)
	for _, def := range defs {
		if state, ok := raw[def.APIName]; def.APIName != "" && ok {
			out[def.ImageName] = state
		}
	}
	return out
}
