package bills

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"regexp"
	"strings"
	"time"

	"golang.org/x/net/publicsuffix"
	"holdexar/server-go/internal/account"
)

const (
	steamStoreURL   = "https://store.steampowered.com"
	historyPath     = "/account/history/"
	historyMorePath = "/account/AjaxLoadMoreHistory/"
	licensesPath    = "/account/licenses/"
	billPageLimit   = 100
	billBodyLimit   = 8 << 20
)

var (
	errStoreExpired = errors.New("Steam 商店会话已失效，请重新绑定后同步账单")
	errStoreOwner   = errors.New("Steam 账单会话与目标账号不一致")
	historyCursor   = regexp.MustCompile(`\bg_historyCursor\s*=\s*\{`)
	billPageTitle   = regexp.MustCompile(`(?is)<title\b[^>]*>(.*?)</title>`)
)

type billSteamClient struct {
	base   *url.URL
	owner  string
	client *http.Client
	sleep  func(context.Context, time.Duration) error
}

func sameStore(a, b *url.URL) bool {
	return a.Scheme == b.Scheme && strings.EqualFold(a.Host, b.Host) && b.User == nil
}

func newBillSteamClient(base *url.URL, transport *http.Client, raw string, sleep func(context.Context, time.Duration) error) (*billSteamClient, error) {
	if base.Scheme != "https" || base.Host == "" || base.User != nil {
		return nil, errors.New("Steam 商店地址无效")
	}
	owner := account.CookieSteamID(raw)
	if owner == "" {
		return nil, errStoreOwner
	}
	jar, err := cookiejar.New(&cookiejar.Options{PublicSuffixList: publicsuffix.List})
	if err != nil {
		return nil, err
	}
	cookies := []*http.Cookie{{Name: "birthtime", Value: "283993201"}, {Name: "wants_mature_content", Value: "1"}}
	values := account.ParseCookies(raw)
	for _, name := range []string{"steamLoginSecure", "steamCountry", "sessionid"} {
		if value := values[name]; value != "" {
			cookies = append(cookies, &http.Cookie{Name: name, Value: value})
		}
	}
	jar.SetCookies(base, cookies)
	client := &http.Client{Transport: transport.Transport, Timeout: transport.Timeout, Jar: jar}
	client.CheckRedirect = func(req *http.Request, via []*http.Request) error {
		if loginPageURL(req.URL) {
			return errStoreExpired
		}
		if !sameStore(base, req.URL) || len(via) >= 10 {
			return errors.New("Steam 账单重定向地址无效")
		}
		return checkStoreOwner(jar, req.URL, owner)
	}
	if sleep == nil {
		sleep = func(ctx context.Context, delay time.Duration) error {
			timer := time.NewTimer(delay)
			defer timer.Stop()
			select {
			case <-timer.C:
				return nil
			case <-ctx.Done():
				return ctx.Err()
			}
		}
	}
	return &billSteamClient{base: base, owner: owner, client: client, sleep: sleep}, nil
}

func checkStoreOwner(jar http.CookieJar, target *url.URL, owner string) error {
	found := false
	for _, cookie := range jar.Cookies(target) {
		if cookie.Name == "steamLoginSecure" {
			if account.CookieSteamID(cookie.String()) != owner {
				return errStoreOwner
			}
			found = true
		}
	}
	if !found {
		return errStoreExpired
	}
	return nil
}

func loginPageURL(target *url.URL) bool {
	path := strings.ToLower(target.Path)
	return strings.Contains(path, "/login") || strings.Contains(path, "/signin") || strings.EqualFold(target.Hostname(), "login.steampowered.com")
}

func (c *billSteamClient) storeURL(path string) *url.URL {
	return c.base.ResolveReference(&url.URL{Path: path})
}

func billRequest(ctx context.Context, client *http.Client, target *url.URL, form url.Values, limit int64) ([]byte, error) {
	method := http.MethodGet
	var body io.Reader
	if form != nil {
		method = http.MethodPost
		body = strings.NewReader(form.Encode())
	}
	request, err := http.NewRequestWithContext(ctx, method, target.String(), body)
	if err != nil {
		return nil, errors.New("Steam 账单请求地址无效")
	}
	request.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
	request.Header.Set("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
	if form != nil {
		request.Header.Set("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
	}
	response, err := client.Do(request)
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		if errors.Is(err, errStoreExpired) {
			return nil, errStoreExpired
		}
		if errors.Is(err, errStoreOwner) {
			return nil, errStoreOwner
		}
		return nil, errors.New("无法安全连接 Steam 账单服务")
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("Steam 账单请求失败（HTTP %d）", response.StatusCode)
	}
	raw, err := io.ReadAll(io.LimitReader(response.Body, limit+1))
	if err != nil || int64(len(raw)) > limit {
		return nil, errors.New("Steam 账单响应不完整或过大")
	}
	return raw, nil
}

func (c *billSteamClient) get(ctx context.Context, target *url.URL, form url.Values) (string, error) {
	if err := checkStoreOwner(c.client.Jar, target, c.owner); err != nil {
		return "", err
	}
	raw, err := billRequest(ctx, c.client, target, form, billBodyLimit)
	if err != nil {
		return "", err
	}
	if err := checkStoreOwner(c.client.Jar, target, c.owner); err != nil {
		return "", err
	}
	if title := billPageTitle.FindSubmatch(raw[:min(len(raw), 8192)]); len(title) == 2 {
		text := strings.ToLower(string(title[1]))
		if strings.Contains(text, "sign in") || strings.Contains(text, "登录") || strings.Contains(text, "登入") {
			return "", errStoreExpired
		}
	}
	return string(raw), nil
}

func (c *billSteamClient) probe(ctx context.Context, known string) (first string, unchanged bool, err error) {
	if known == "" {
		return "", false, nil
	}
	page, err := c.get(ctx, c.storeURL(historyPath), nil)
	if err != nil {
		if errors.Is(err, errStoreExpired) || errors.Is(err, errStoreOwner) || ctx.Err() != nil {
			return "", false, err
		}
		return "", false, nil
	}
	rows, err := parseHistoryHTML(page, true)
	if err != nil || len(rows) == 0 {
		return page, false, nil
	}
	date := parseDate(rows[0].Date)
	_, err = time.Parse(time.DateOnly, date)
	return page, err == nil && date <= known, nil
}

func decodeCursor(raw json.RawMessage) (url.Values, error) {
	if len(raw) == 0 {
		return nil, nil
	}
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.UseNumber()
	var fields map[string]any
	if err := decoder.Decode(&fields); err != nil {
		return nil, errors.New("Steam 消费明细游标无法读取")
	}
	form := url.Values{}
	for key, field := range fields {
		switch field := field.(type) {
		case string, json.Number:
			form.Set("cursor["+key+"]", fmt.Sprint(field))
		case bool:
			form.Set("cursor["+key+"]", map[bool]string{true: "True", false: "False"}[field])
		case nil:
			form.Set("cursor["+key+"]", "None")
		default:
			return nil, errors.New("Steam 消费明细游标格式无效")
		}
	}
	return form, nil
}

func (c *billSteamClient) history(ctx context.Context, first string, progress func(map[string]any)) ([]historyRow, error) {
	if first == "" {
		var err error
		first, err = c.get(ctx, c.storeURL(historyPath), nil)
		if err != nil {
			return nil, err
		}
	}
	rows, err := parseHistoryHTML(first, true)
	if err != nil {
		return nil, err
	}
	progress(map[string]any{"stage": "history", "pages": 0, "rows": len(rows)})
	var cursor json.RawMessage
	if match := historyCursor.FindStringIndex(first); match != nil {
		if err := json.NewDecoder(strings.NewReader(first[match[1]-1:])).Decode(&cursor); err != nil {
			return nil, errors.New("Steam 消费明细游标无法读取")
		}
	}
	seen := map[string]bool{}
	for page := 1; ; page++ {
		form, err := decodeCursor(cursor)
		if err != nil || len(form) == 0 {
			return rows, err
		}
		key := form.Encode()
		if page > billPageLimit || seen[key] {
			return nil, errors.New("Steam 消费明细翻页未完成，保留已有账单")
		}
		seen[key] = true
		target := c.storeURL(historyMorePath)
		for _, cookie := range c.client.Jar.Cookies(target) {
			if cookie.Name == "sessionid" {
				form.Set("sessionid", cookie.Value)
				break
			}
		}
		if form.Get("sessionid") == "" {
			return nil, errors.New("Steam 消费明细缺少翻页会话")
		}
		response, err := c.get(ctx, target, form)
		if err != nil {
			return nil, err
		}
		var payload struct {
			HTML    json.RawMessage `json:"html"`
			Cursor  json.RawMessage `json:"cursor"`
			Success *bool           `json:"success"`
		}
		var fragment string
		if json.Unmarshal([]byte(response), &payload) != nil || json.Unmarshal(payload.HTML, &fragment) != nil || payload.Success != nil && !*payload.Success {
			return nil, errors.New("Steam 消费明细翻页响应无法读取")
		}
		next, err := parseHistoryHTML(fragment, false)
		if err != nil {
			return nil, err
		}
		rows = append(rows, next...)
		cursor = payload.Cursor
		if len(next) == 0 {
			form, err := decodeCursor(cursor)
			if err != nil || len(form) != 0 {
				return nil, errors.New("Steam 消费明细翻页内容不完整")
			}
			return rows, nil
		}
		progress(map[string]any{"stage": "history", "pages": page, "rows": len(rows)})
		if len(cursor) != 0 && string(cursor) != "null" {
			if err := c.sleep(ctx, 150*time.Millisecond); err != nil {
				return nil, err
			}
		}
	}
}

func (c *billSteamClient) licenses(ctx context.Context) ([]licenseRow, error) {
	start := c.storeURL(licensesPath)
	target := start
	seen := map[string]bool{}
	var rows []licenseRow
	for page := 1; ; page++ {
		if page > billPageLimit || seen[target.String()] {
			return nil, errors.New("Steam 游戏许可翻页未完成，保留已有账单")
		}
		seen[target.String()] = true
		body, err := c.get(ctx, target, nil)
		if err != nil {
			return nil, err
		}
		nextRows, href, err := parseLicenseHTML(body)
		if err != nil {
			return nil, err
		}
		rows = append(rows, nextRows...)
		if href == "" {
			return rows, nil
		}
		if len(nextRows) == 0 {
			return nil, errors.New("Steam 游戏许可翻页内容不完整")
		}
		target, err = start.Parse(href)
		if err != nil || !sameStore(c.base, target) || target.Path != licensesPath {
			return nil, errors.New("Steam 游戏许可翻页地址无效")
		}
		if err := c.sleep(ctx, 150*time.Millisecond); err != nil {
			return nil, err
		}
	}
}

func (c *billSteamClient) avatar(ctx context.Context, raw string) string {
	target, err := url.Parse(account.NormalizeAvatar(raw))
	if err != nil || target.Scheme != "https" || target.User != nil || target.Hostname() != "avatars.fastly.steamstatic.com" || target.Port() != "" {
		return ""
	}
	public := &http.Client{Transport: c.client.Transport, Timeout: c.client.Timeout, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	data, err := billRequest(ctx, public, target, nil, 1<<20)
	if err != nil {
		return ""
	}
	mime := http.DetectContentType(data)
	if mime != "image/png" && mime != "image/jpeg" {
		return ""
	}
	return "data:" + mime + ";base64," + base64.StdEncoding.EncodeToString(data)
}

func (c *billSteamClient) report(ctx context.Context, sid, first string, progress func(map[string]any)) (steamReport, error) {
	result := steamReport{Nickname: "Steam 账户 " + sid}
	if body, err := c.get(ctx, c.storeURL("/account/"), nil); err == nil {
		name, image, _ := parseIdentityHTML(body)
		if name != "" {
			result.Nickname = name
		}
		result.Avatar = c.avatar(ctx, image)
	} else if errors.Is(err, errStoreExpired) || errors.Is(err, errStoreOwner) {
		return steamReport{}, err
	}
	progress(map[string]any{"stage": "identity"})
	var err error
	result.History, err = c.history(ctx, first, progress)
	if err != nil {
		return steamReport{}, err
	}
	result.Licenses, err = c.licenses(ctx)
	if err != nil {
		return steamReport{}, err
	}
	progress(map[string]any{"stage": "licenses"})
	if len(result.History) == 0 && len(result.Licenses) == 0 {
		return steamReport{}, errors.New("Steam 返回空账单和许可，保留已有账单")
	}
	return result, nil
}
