package humblechoice

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode"

	"holdexar/server-go/internal/netenv"
)

const membershipURL = "https://www.humblebundle.com/membership"
const steamSearchURL = "https://store.steampowered.com/api/storesearch"

// flightPush 提取页面内嵌的 RSC flight 文本流。
var flightPush = regexp.MustCompile(`self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)`)
var monthDesc = regexp.MustCompile(`Get ([A-Za-z]+) (\d{4}) Humble Choice`)
var monthKey = regexp.MustCompile(`^([a-z]+)_(\d{4})_choice$`)
var productMonth = regexp.MustCompile(`([A-Za-z]+)\s+(\d{4})`)
var edition = regexp.MustCompile(`\s*-\s*pc edition|\s*\(pc\)|\s*\(windows\)|\s*windows edition|\s*xbox one|\s*xbox series x\|s|\s*standard edition|\s*game of the year edition|\s*anniversary edition|\s*complete edition|\s*deluxe edition|\s*definitive edition|\s*remastered|\s*director's cut|\s*ultimate edition`)
var searchWords = regexp.MustCompile(`[^\pL\pN_\s]+`)
var dlcBundleSuffix = regexp.MustCompile(`(?i)\s*\+\s*\d+\s+dlc packs?\s*$`)

type month struct {
	Machine string
	Product string
	End     string
	Titles  []string
	Others  []platformGame
}

type platformGame struct {
	Title     string `json:"title"`
	Platform  string `json:"platform"`
	Thumbnail string `json:"thumbnail,omitempty"`
	RetailFen *int64 `json:"retailFen,omitempty"`
	EndDate   string `json:"endDate,omitempty"`
}

type choiceProduct struct {
	ID           string   `json:"id"`
	Title        string   `json:"title"`
	Delivery     []string `json:"deliveryMethods"`
	EndDate      string   `json:"endDate"`
	Thumbnail    string   `json:"thumbnail"`
	DisplayPrice struct {
		CurrentPrice struct {
			CentAmount float64 `json:"centAmount"`
		} `json:"currentPrice"`
	} `json:"displayPrice"`
}

// flightPayload 解码各段 RSC push 字符串并拼接，单段解码失败跳过。
func flightPayload(page string) string {
	var full strings.Builder
	for _, match := range flightPush.FindAllStringSubmatch(page, -1) {
		var decoded string
		if json.Unmarshal([]byte("\""+match[1]+"\""), &decoded) != nil {
			continue
		}
		full.WriteString(decoded)
	}
	return full.String()
}

// extractJSONValue 提取 key 后第一个完整 JSON 数组（配平引号、转义与方括号）。
func extractJSONValue(payload, key string) (string, bool) {
	i := strings.Index(payload, key)
	if i < 0 {
		return "", false
	}
	start := i + len(key)
	for start < len(payload) && (payload[start] == ' ' || payload[start] == ':') {
		start++
	}
	if start >= len(payload) || payload[start] != '[' {
		return "", false
	}
	depth, inStr, esc := 0, false, false
	for j := start; j < len(payload); j++ {
		ch := payload[j]
		if inStr {
			switch {
			case esc:
				esc = false
			case ch == '\\':
				esc = true
			case ch == '"':
				inStr = false
			}
			continue
		}
		switch ch {
		case '"':
			inStr = true
		case '[':
			depth++
		case ']':
			depth--
			if depth == 0 {
				return payload[start : j+1], true
			}
		}
	}
	return "", false
}

// parseMembership 解析 subscriptionProducts 与期号：Steam 进 Titles、其余平台进 Others、仅 external_key 排除。
func parseMembership(raw []byte) (month, error) {
	payload := flightPayload(string(raw))
	if payload == "" {
		return month{}, errors.New("页面无内嵌数据（请求被识别为机器人，返回了空壳）")
	}
	source, ok := extractJSONValue(payload, `"subscriptionProducts"`)
	if !ok {
		return month{}, errors.New("页面无当月包产品清单")
	}
	var products []choiceProduct
	if json.Unmarshal([]byte(source), &products) != nil {
		return month{}, errors.New("当月包产品清单格式无效")
	}
	m := month{}
	seen := map[string]bool{}
	for _, p := range products {
		title := strings.TrimSpace(p.Title)
		if title == "" {
			continue
		}
		steam := false
		platform := ""
		for _, d := range p.Delivery {
			if d == "steam" {
				steam = true
				break
			}
			if d != "external_key" && platform == "" {
				platform = d
			}
		}
		if m.End == "" && p.EndDate != "" {
			m.End = p.EndDate
		}
		if seen[title] {
			continue
		}
		if steam {
			m.Titles = append(m.Titles, title)
			seen[title] = true
			continue
		}
		if platform == "" {
			continue
		}
		var retail *int64
		if cents := int64(p.DisplayPrice.CurrentPrice.CentAmount + 0.5); cents > 0 {
			retail = &cents
		}
		m.Others = append(m.Others, platformGame{
			Title: title, Platform: platform,
			Thumbnail: p.Thumbnail, RetailFen: retail, EndDate: p.EndDate,
		})
		seen[title] = true
	}
	match := monthDesc.FindStringSubmatch(payload)
	if match == nil {
		return month{}, errors.New("页面无当月期号（meta 描述缺失）")
	}
	m.Machine = strings.ToLower(match[1]) + "_" + match[2] + "_choice"
	m.Product = match[1] + " " + match[2]
	if len(m.Titles) == 0 && len(m.Others) == 0 {
		return month{}, errors.New("页面无当月包游戏数据")
	}
	return m, nil
}

func parseMonth(raw string) time.Month {
	raw = strings.ToLower(raw)
	if len(raw) < 3 {
		return 0
	}
	for i := time.January; i <= time.December; i++ {
		if strings.ToLower(i.String()[:3]) == raw[:3] {
			return i
		}
	}
	return 0
}

func monthLabel(m month) string {
	for _, candidate := range []string{m.Product, m.Machine} {
		match := monthKey.FindStringSubmatch(strings.ToLower(candidate))
		if len(match) > 0 {
			if mon := parseMonth(match[1]); mon != 0 {
				return fmt.Sprintf("HB慈善包%s年%d月包", match[2][2:], mon)
			}
		}
	}
	if match := productMonth.FindStringSubmatch(m.Product); len(match) > 0 {
		if mon := parseMonth(match[1]); mon != 0 {
			return fmt.Sprintf("HB慈善包%s年%d月包", match[2][2:], mon)
		}
	}
	return "HB慈善包"
}

func monthURL(machine string) string {
	return membershipURL
}

func publicClient(proxy func(context.Context) (string, bool)) *http.Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = func(r *http.Request) (*url.URL, error) {
		// 优先使用注入 proxy，不可用回落直连；proxy 为 nil 时读系统代理。
		if proxy != nil {
			if raw, ok := proxy(r.Context()); ok && raw != "" {
				return url.Parse(raw)
			}
			return nil, nil
		}
		raw, err := netenv.ResolveSystemProxy(r.Context())
		if err != nil || raw == "" {
			return nil, err
		}
		return url.Parse(raw)
	}
	return &http.Client{Timeout: 25 * time.Second, Transport: transport, CheckRedirect: func(r *http.Request, via []*http.Request) error {
		if len(via) >= 5 || r.URL.Scheme != "https" || r.URL.Host != via[0].URL.Host || r.URL.User != nil {
			return http.ErrUseLastResponse
		}
		return nil
	}}
}

func (s *Service) request(ctx context.Context, address string, limit int64) ([]byte, error) {
	r, err := http.NewRequestWithContext(ctx, http.MethodGet, address, nil)
	if err != nil {
		return nil, errors.New("公开来源地址无效")
	}
	host := ""
	if parsed, err := url.Parse(address); err == nil {
		host = parsed.Hostname()
	}
	if host == "www.humblebundle.com" {
		// 会员页须携带 Sec-Fetch 与 sec-ch-ua 指纹头，否则服务端返回空壳页。
		r.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8")
		r.Header.Set("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
		r.Header.Set("Upgrade-Insecure-Requests", "1")
		r.Header.Set("Sec-Fetch-Dest", "document")
		r.Header.Set("Sec-Fetch-Mode", "navigate")
		r.Header.Set("Sec-Fetch-Site", "none")
		r.Header.Set("Sec-Fetch-User", "?1")
		r.Header.Set("sec-ch-ua", `"Chromium";v="152", "Not?A_Brand";v="24"`)
		r.Header.Set("sec-ch-ua-mobile", "?0")
		r.Header.Set("sec-ch-ua-platform", `"Windows"`)
	} else {
		// Steam JSON API 须使用 JSON Accept 头，携带导航头会被对端断连。
		r.Header.Set("Accept", "application/json, text/html;q=0.9, */*;q=0.8")
	}
	r.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
	resp, err := s.opts.Client.Do(r)
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		return nil, errors.New("公开来源连接失败")
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("公开来源返回 HTTP %d", resp.StatusCode)
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
	if err != nil || int64(len(raw)) > limit {
		return nil, errors.New("公开来源数据不完整")
	}
	return raw, nil
}

type searchHit struct {
	ID   json.RawMessage `json:"id"`
	Name string          `json:"name"`
}

func cleanName(name string) string {
	name = strings.ToLower(name)
	name = strings.NewReplacer("™", "", "®", "", "©", "", "’", "'", "–", "-", "—", "-", "ea sports fc", "fc", "ea sports ", "").Replace(name)
	name = strings.Map(func(r rune) rune {
		if unicode.IsLetter(r) || unicode.IsNumber(r) || unicode.IsSpace(r) || r == '_' {
			return r
		}
		return -1
	}, edition.ReplaceAllString(name, ""))
	return strings.Join(strings.Fields(name), " ")
}

func pickAppID(title string, hits []searchHit, exactOnly bool) int64 {
	want := strings.TrimSpace(title)
	clean := cleanName(want)
	var stripped []string
	for _, word := range strings.Fields(clean) {
		if word != "playtest" {
			stripped = append(stripped, word)
		}
	}
	playtest := strings.Join(stripped, " ")
	var cleanedID, playtestID int64
	for _, hit := range hits {
		id, _ := strconv.ParseInt(strings.Trim(string(hit.ID), `"`), 10, 64)
		if id <= 0 || strings.TrimSpace(hit.Name) == "" {
			continue
		}
		if strings.EqualFold(strings.TrimSpace(hit.Name), want) {
			return id
		}
		if exactOnly {
			continue
		}
		hitClean := cleanName(hit.Name)
		if cleanedID == 0 && clean != "" && hitClean == clean {
			cleanedID = id
		}
		if playtestID == 0 && playtest != "" && playtest != clean && hitClean == playtest {
			playtestID = id
		}
	}
	if cleanedID != 0 {
		return cleanedID
	}
	return playtestID
}

func (s *Service) resolve(ctx context.Context, title string) int64 {
	base := strings.TrimSpace(dlcBundleSuffix.ReplaceAllString(title, ""))
	exactOnly := base != title
	title = base
	words := strings.Fields(searchWords.ReplaceAllString(title, " "))
	if len(words) == 0 {
		return 0
	}
	terms := []string{strings.Join(words, " ")}
	if len(words) > 2 {
		terms = append(terms, strings.Join(words[:2], " "))
	}
	if len(words) > 1 {
		terms = append(terms, words[0])
	}
	for _, term := range terms {
		address := s.opts.SearchURL + "?" + (url.Values{"term": {term}, "cc": {"us"}, "l": {"en"}}).Encode()
		var raw []byte
		var err error
		for attempt := 0; attempt < 2; attempt++ {
			if attempt > 0 {
				select {
				case <-time.After(1500 * time.Millisecond):
				case <-ctx.Done():
					return 0
				}
			}
			fetch, cancel := context.WithTimeout(ctx, 15*time.Second)
			raw, err = s.request(fetch, address, 1<<20)
			cancel()
			if err == nil {
				break
			}
			if ctx.Err() != nil {
				return 0
			}
		}
		if err != nil {
			continue
		}
		var result struct{ Items []searchHit }
		if json.Unmarshal(raw, &result) != nil {
			continue
		}
		if len(result.Items) > 0 {
			return pickAppID(title, result.Items, exactOnly)
		}
	}
	return 0
}
