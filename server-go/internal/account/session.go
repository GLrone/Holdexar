package account

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/url"
	"strings"
	"time"
)

func (c *SteamClient) sleep(ctx context.Context, d time.Duration) error {
	if c.opts.Sleep != nil {
		return c.opts.Sleep(ctx, d)
	}
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}
func (c *SteamClient) trustedTransfer(raw string) bool {
	u, e := url.Parse(raw)
	if e != nil || u.User != nil || u.Host == "" {
		return false
	}
	host := strings.ToLower(u.Hostname())
	if u.Scheme == "https" && (host == "steamcommunity.com" || host == "steampowered.com" || strings.HasSuffix(host, ".steampowered.com")) {
		return true
	}
	base, e := url.Parse(c.opts.FinalizeURL)
	return e == nil && u.Scheme == base.Scheme && u.Host == base.Host
}
func (c *SteamClient) postSession(ctx context.Context, client *http.Client, endpoint string, fields map[string]string) (*http.Response, error) {
	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	for k, v := range fields {
		if e := writer.WriteField(k, v); e != nil {
			return nil, e
		}
	}
	if e := writer.Close(); e != nil {
		return nil, e
	}
	req, e := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, &body)
	if e != nil {
		return nil, e
	}
	req.Header.Set("Content-Type", writer.FormDataContentType())
	req.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
	req.Header.Set("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
	req.Header.Set("Origin", "https://steamcommunity.com")
	req.Header.Set("Referer", "https://steamcommunity.com/")
	return client.Do(req)
}
func responseCookies(resp *http.Response) map[string]string {
	values := map[string]string{}
	for _, cookie := range resp.Cookies() {
		values[cookie.Name] = cookie.Value
	}
	return values
}
func (c *SteamClient) RefreshSession(ctx context.Context, raw string) (string, error) {
	sid := CookieSteamID(raw)
	if sid == "" {
		sid, _ = cookieToken(ParseCookies(raw)["steamRefresh_steam"])
	}
	if !numericID(sid) {
		return "", walletError("session", "无法从 Cookie 解析 SteamID64")
	}
	token := RefreshToken(raw)
	if token == "" {
		return "", walletError("session", "缺少续期凭据 steamRefresh_steam")
	}
	if owner, _ := cookieToken(ParseCookies(raw)["steamRefresh_steam"]); owner != "" && owner != sid {
		return "", walletError("owner", "续期凭据与 Steam 账号不一致")
	}
	return c.WebCookies(ctx, token, sid, ParseCookies(raw))
}
func (c *SteamClient) WebCookies(ctx context.Context, refresh, sid string, inherited map[string]string) (string, error) {
	if !numericID(sid) || refresh == "" {
		return "", walletError("session", "Steam 续期参数无效")
	}
	client, e := c.client(ctx)
	if e != nil {
		return "", e
	}
	copy := *client
	copy.Timeout = 20 * time.Second
	copy.CheckRedirect = func(req *http.Request, via []*http.Request) error {
		if len(via) >= 10 || !c.trustedTransfer(req.URL.String()) {
			return walletError("session", "Steam 登录跳转地址不可信")
		}
		return nil
	}
	client = &copy
	seed := make([]byte, 12)
	if _, e = rand.Read(seed); e != nil {
		return "", e
	}
	sessionID := hex.EncodeToString(seed)
	var resp *http.Response
	for attempt := 0; attempt < 3; attempt++ {
		resp, e = c.postSession(ctx, client, c.opts.FinalizeURL, map[string]string{"nonce": refresh, "sessionid": sessionID, "redir": "https://steamcommunity.com/login/home/?goto="})
		if e == nil {
			break
		}
		if ctx.Err() != nil {
			return "", ctx.Err()
		}
		if attempt < 2 {
			if e = c.sleep(ctx, time.Duration(attempt+1)*800*time.Millisecond); e != nil {
				return "", e
			}
		}
	}
	if e != nil {
		return "", walletError("session", "Steam 续期连接失败，本轮保留原凭据")
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return "", walletError("session", fmt.Sprintf("续期被 Steam 拒绝（HTTP %d）", resp.StatusCode))
	}
	var body struct {
		Error     json.RawMessage `json:"error"`
		Transfers []struct {
			URL    string         `json:"url"`
			Params map[string]any `json:"params"`
		} `json:"transfer_info"`
	}
	decoder := json.NewDecoder(io.LimitReader(resp.Body, 1<<20))
	decoder.UseNumber()
	if decoder.Decode(&body) != nil || len(body.Transfers) == 0 {
		return "", walletError("session", "Steam 续期响应缺少有效 Cookie 下发信息")
	}
	if len(body.Error) > 0 && string(body.Error) != "0" && string(body.Error) != "null" && string(body.Error) != "false" && string(body.Error) != `""` {
		return "", walletError("session", "Steam 续期请求未成功")
	}
	values := responseCookies(resp)
	accessByHost := map[string]string{}
	hostOrder := []string{}
	for _, transfer := range body.Transfers {
		if transfer.URL == "" {
			continue
		}
		if !c.trustedTransfer(transfer.URL) {
			return "", walletError("session", "Steam Cookie 下发地址不可信")
		}
		fields := map[string]string{"steamID": sid}
		for k, v := range transfer.Params {
			fields[k] = fmt.Sprint(v)
		}
		fields["steamID"] = sid
		var got map[string]string
		for attempt := 0; attempt < 4; attempt++ {
			response, e := c.postSession(ctx, client, transfer.URL, fields)
			if e == nil {
				response.Body.Close()
				if response.StatusCode < 400 {
					got = responseCookies(response)
					if got["steamLoginSecure"] != "" {
						break
					}
				}
			}
			if ctx.Err() != nil {
				return "", ctx.Err()
			}
			if attempt < 3 {
				if e = c.sleep(ctx, time.Duration(attempt+1)*time.Second); e != nil {
					return "", e
				}
			}
		}
		if got["steamLoginSecure"] == "" {
			return "", walletError("session", "Steam Cookie 下发失败，本轮保留原凭据")
		}
		owner, _ := cookieToken(got["steamLoginSecure"])
		if owner != sid {
			return "", walletError("owner", "Steam 续期响应属于其他账号，已拒绝保存")
		}
		u, _ := url.Parse(transfer.URL)
		host := u.Hostname()
		if accessByHost[host] == "" {
			accessByHost[host] = got["steamLoginSecure"]
			hostOrder = append(hostOrder, host)
		}
		for k, v := range got {
			values[k] = v
		}
	}
	for _, host := range append([]string{"store.steampowered.com", "steamcommunity.com"}, hostOrder...) {
		if v := accessByHost[host]; v != "" {
			values["steamLoginSecure"] = v
			break
		}
	}
	if values["steamLoginSecure"] == "" {
		return "", walletError("session", "Steam 续期未返回访问凭据")
	}
	if values["steamRefresh_steam"] == "" {
		values["steamRefresh_steam"] = sid + "||" + refresh
	}
	if owner, _ := cookieToken(values["steamRefresh_steam"]); owner != "" && owner != sid {
		return "", walletError("owner", "Steam 续期凭据属于其他账号")
	}
	values["sessionid"] = sessionID
	values["steamCountry"] = inherited["steamCountry"]
	values["steamRememberLogin"] = inherited["steamRememberLogin"]
	parts := []string{}
	for _, k := range loginCookieKeys {
		if values[k] != "" {
			parts = append(parts, k+"="+values[k])
		}
	}
	return strings.Join(parts, "; "), nil
}
