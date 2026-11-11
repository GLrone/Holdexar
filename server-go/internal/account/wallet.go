package account

import (
	"context"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"io"
	"math"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"
)

type WalletError struct{ Kind, Message string }

func (e *WalletError) Error() string         { return e.Message }
func walletError(kind, message string) error { return &WalletError{kind, message} }

type Wallet struct {
	Balance                                               float64
	BalanceDisplay, CurrencyCode, CurrencySymbol, SteamID string
	CurrencyID                                            int
}
type steamCurrency struct{ code, symbol string }

var steamCurrencies = map[int]steamCurrency{
	1: {"USD", "$"}, 2: {"GBP", "£"}, 3: {"EUR", "€"}, 5: {"RUB", "₽"}, 6: {"PLN", "zł"}, 7: {"BRL", "R$"}, 8: {"JPY", "¥"}, 9: {"NOK", "kr"}, 10: {"IDR", "Rp"}, 11: {"MYR", "RM"}, 12: {"PHP", "₱"}, 13: {"SGD", "S$"}, 14: {"THB", "฿"}, 15: {"VND", "₫"}, 16: {"KRW", "₩"}, 17: {"TRY", "₺"}, 18: {"UAH", "₴"}, 19: {"MXN", "Mex$"}, 20: {"CAD", "C$"}, 21: {"AUD", "A$"}, 22: {"NZD", "NZ$"}, 23: {"CNY", "¥"}, 24: {"INR", "₹"}, 25: {"CLP", "CLP$"}, 26: {"PEN", "S/"}, 27: {"COP", "COL$"}, 28: {"ZAR", "R"}, 29: {"HKD", "HK$"}, 30: {"TWD", "NT$"}, 31: {"SAR", "SR"}, 32: {"AED", "AED"}, 34: {"ARS", "ARS$"}, 35: {"ILS", "₪"}, 37: {"KZT", "₸"}, 38: {"KWD", "KD"}, 39: {"QAR", "QR"},
}
var balancePattern = regexp.MustCompile(`(?s)class=["']accountRow accountBalance["'][^>]*>\s*<div[^>]*class=["']accountData price["'][^>]*>(.*?)</div>`)
var currencyPattern = regexp.MustCompile(`"currency_code"\s*:\s*(\d+)\s*,\s*"formatted_amount"\s*:\s*"((?:\\.|[^"\\])*)"`)
var tagsPattern = regexp.MustCompile(`<[^>]+>`)
var anchorPattern = regexp.MustCompile(`(?i)<a\b[^>]*>`)
var hrefPattern = regexp.MustCompile(`(?i)href=["']([^"']+)["']`)
var profileOwnerPattern = regexp.MustCompile(`^/profiles/([0-9]{17,})/?$`)

func moneyNumber(raw string) (float64, error) {
	var b strings.Builder
	for _, r := range raw {
		if (r >= '0' && r <= '9') || r == '.' || r == ',' {
			b.WriteRune(r)
		} else if unicode.IsSpace(r) {
			continue
		}
	}
	s := b.String()
	comma, dot := strings.LastIndex(s, ","), strings.LastIndex(s, ".")
	if comma >= 0 && dot >= 0 {
		decimal, separator := ".", ","
		at := dot
		if comma > dot {
			decimal, separator = ",", "."
			at = comma
		}
		s = strings.ReplaceAll(s[:at], separator, "") + "." + s[at+len(decimal):]
	} else if comma >= 0 || dot >= 0 {
		at, separator := comma, ","
		if dot >= 0 {
			at, separator = dot, "."
		}
		if len(s)-at-1 == 1 || len(s)-at-1 == 2 {
			s = strings.ReplaceAll(s[:at], separator, "") + "." + s[at+1:]
		} else {
			s = strings.ReplaceAll(s, separator, "")
		}
	}
	v, e := strconv.ParseFloat(s, 64)
	if e != nil || math.IsInf(v, 0) || math.IsNaN(v) {
		return 0, walletError("format", "无法识别 Steam 钱包余额")
	}
	return math.RoundToEven(v*100) / 100, nil
}
func groupedAmount(v float64, whole bool) string {
	s := fmt.Sprintf("%.2f", v)
	if whole {
		s = strconv.FormatInt(int64(v), 10)
	}
	parts := strings.SplitN(s, ".", 2)
	head := parts[0]
	for i := len(head) - 3; i > 0; i -= 3 {
		head = head[:i] + "," + head[i:]
	}
	if len(parts) > 1 {
		return head + "." + parts[1]
	}
	return head
}
func ParseWalletPage(page, expected string) (Wallet, error) {
	var result Wallet
	bal := balancePattern.FindStringSubmatch(page)
	cur := currencyPattern.FindStringSubmatch(html.UnescapeString(page))
	if len(bal) < 2 || len(cur) < 3 {
		return result, walletError("format", "无法获取 Steam 钱包余额（账户页缺少余额或币种）")
	}
	id, e := strconv.Atoi(cur[1])
	if e != nil {
		return result, walletError("currency", "Steam 钱包币种无效")
	}
	currency, ok := steamCurrencies[id]
	if !ok {
		return result, walletError("currency", fmt.Sprintf("未知 Steam 钱包币种 ID: %d", id))
	}
	raw := strings.TrimSpace(html.UnescapeString(tagsPattern.ReplaceAllString(bal[1], "")))
	var formatted string
	if json.Unmarshal([]byte(`"`+cur[2]+`"`), &formatted) != nil || !strings.Contains(formatted, currency.symbol) || !strings.Contains(raw, currency.symbol) {
		return result, walletError("currency", "余额符号与 Steam 钱包币种不一致")
	}
	amount, e := moneyNumber(raw)
	if e != nil {
		return result, e
	}
	owner := ""
	for _, a := range anchorPattern.FindAllString(page, -1) {
		if !strings.Contains(a, "user_avatar") {
			continue
		}
		match := hrefPattern.FindStringSubmatch(a)
		if len(match) < 2 {
			continue
		}
		u, e := url.Parse(html.UnescapeString(match[1]))
		if e != nil || !strings.EqualFold(u.Hostname(), "steamcommunity.com") {
			continue
		}
		match = profileOwnerPattern.FindStringSubmatch(u.Path)
		if len(match) > 1 {
			owner = match[1]
			break
		}
	}
	if owner == "" || owner != expected {
		return result, walletError("owner", "无法确认钱包属于该 Steam 账号，本次保留已有余额")
	}
	result = Wallet{Balance: amount, BalanceDisplay: currency.symbol + groupedAmount(amount, id == 8 || id == 15 || id == 16), CurrencyCode: currency.code, CurrencySymbol: currency.symbol, CurrencyID: id, SteamID: owner}
	return result, nil
}

type SteamOptions struct {
	HTTPClient                          *http.Client
	AccountURL, ProfileURL, FinalizeURL string
	ProfileXMLURL                       string
	AuthBaseURL, CheckDeviceURL         string
	Proxy                               func(context.Context) (string, error)
	Sleep                               func(context.Context, time.Duration) error
}
type SteamClient struct {
	opts    SteamOptions
	mu      sync.Mutex
	clients map[string]*http.Client
}

func NewSteamClient(opts SteamOptions) *SteamClient {
	if opts.AccountURL == "" {
		opts.AccountURL = "https://store.steampowered.com/account/"
	}
	if opts.ProfileURL == "" {
		opts.ProfileURL = "https://steamcommunity.com/miniprofile/%s/json"
	}
	if opts.ProfileXMLURL == "" {
		opts.ProfileXMLURL = "https://steamcommunity.com/profiles/%s/?xml=1"
	}
	if opts.FinalizeURL == "" {
		opts.FinalizeURL = "https://login.steampowered.com/jwt/finalizelogin"
	}
	if opts.AuthBaseURL == "" {
		opts.AuthBaseURL = "https://api.steampowered.com/IAuthenticationService"
	}
	if opts.CheckDeviceURL == "" {
		opts.CheckDeviceURL = "https://login.steampowered.com/jwt/checkdevice"
	}
	return &SteamClient{opts: opts, clients: map[string]*http.Client{}}
}
func (c *SteamClient) client(ctx context.Context) (*http.Client, error) {
	if c.opts.HTTPClient != nil {
		copy := *c.opts.HTTPClient
		copy.Jar = nil
		return &copy, nil
	}
	proxy := ""
	if c.opts.Proxy != nil {
		var e error
		proxy, e = c.opts.Proxy(ctx)
		if e != nil {
			return nil, walletError("network", "Steam 代理连接尚未就绪")
		}
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if cached := c.clients[proxy]; cached != nil {
		return cached, nil
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	if proxy != "" {
		u, e := url.Parse(proxy)
		if e != nil {
			return nil, walletError("network", "Steam 代理配置无效")
		}
		transport.Proxy = http.ProxyURL(u)
	}
	client := &http.Client{Transport: transport, Timeout: 15 * time.Second}
	c.clients[proxy] = client
	return client, nil
}

func (c *SteamClient) StoreHTTPClient(ctx context.Context) (*http.Client, error) {
	client, err := c.client(ctx)
	if err != nil {
		return nil, err
	}
	return &http.Client{Transport: client.Transport, Timeout: 30 * time.Second}, nil
}

func (c *SteamClient) FetchWallet(ctx context.Context, cookies string) (Wallet, error) {
	sid := CookieSteamID(cookies)
	if sid == "" {
		return Wallet{}, walletError("auth", "Steam Cookie 未配置或缺少 steamLoginSecure")
	}
	client, e := c.client(ctx)
	if e != nil {
		return Wallet{}, e
	}
	for attempt := 0; attempt < 2; attempt++ {
		req, e := http.NewRequestWithContext(ctx, http.MethodGet, c.opts.AccountURL, nil)
		if e != nil {
			return Wallet{}, walletError("network", "Steam 账户地址无效")
		}
		req.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
		req.Header.Set("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
		req.Header.Set("Cookie", FilterCookies(cookies)+"; birthtime=283993201; wants_mature_content=1")
		resp, e := client.Do(req)
		if e != nil {
			if ctx.Err() != nil {
				return Wallet{}, ctx.Err()
			}
			var unknown x509.UnknownAuthorityError
			var invalid x509.CertificateInvalidError
			var hostname x509.HostnameError
			if errors.As(e, &unknown) || errors.As(e, &invalid) || errors.As(e, &hostname) {
				return Wallet{}, walletError("tls", "Steam 证书校验失败，本次保留已有余额")
			}
			if attempt == 0 {
				client.CloseIdleConnections()
				continue
			}
			return Wallet{}, walletError("network", "无法连接 Steam（网络或代理出口不通）")
		}
		body, readErr := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
		resp.Body.Close()
		if resp.StatusCode == 429 || resp.StatusCode == 403 {
			return Wallet{}, walletError("rate", fmt.Sprintf("Steam 节点限制访问（HTTP %d）", resp.StatusCode))
		}
		if strings.Contains(strings.ToLower(resp.Request.URL.Path), "/login") {
			return Wallet{}, walletError("expired", "Steam 商店会话已失效，正在尝试自动续期")
		}
		if resp.StatusCode != 200 {
			return Wallet{}, walletError("auth", fmt.Sprintf("无法获取 Steam 钱包余额（HTTP %d）", resp.StatusCode))
		}
		if readErr != nil {
			return Wallet{}, walletError("network", "Steam 账户响应中断，本次保留已有余额")
		}
		return ParseWalletPage(string(body), sid)
	}
	return Wallet{}, walletError("network", "无法连接 Steam")
}
func (c *SteamClient) Close() {
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, client := range c.clients {
		client.CloseIdleConnections()
	}
}
