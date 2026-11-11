package humblechoice

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"html"
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

var choiceItem = regexp.MustCompile(`"([a-z0-9_]+)"\s*:\s*\{\s*"recommendation_copy_dict"`)
var monthKey = regexp.MustCompile(`^([a-z]+)_(\d{4})_choice$`)
var productMonth = regexp.MustCompile(`([A-Za-z]+)\s+(\d{4})`)
var fieldPattern = regexp.MustCompile(`"(activeContentMachineName|productHumanName|activeContentEndDate\|datetime)"\s*:\s*"([^"]+)"`)
var edition = regexp.MustCompile(`\s*-\s*pc edition|\s*\(pc\)|\s*\(windows\)|\s*windows edition|\s*xbox one|\s*xbox series x\|s|\s*standard edition|\s*game of the year edition|\s*anniversary edition|\s*complete edition|\s*deluxe edition|\s*definitive edition|\s*remastered|\s*director's cut|\s*ultimate edition`)
var searchWords = regexp.MustCompile(`[^\pL\pN_\s]+`)
var dlcBundleSuffix = regexp.MustCompile(`(?i)\s*\+\s*\d+\s+dlc packs?\s*$`)

type month struct {
	Machine string
	Product string
	End     string
	Titles  []string
}

type choiceItemData struct {
	Title    string   `json:"title"`
	Delivery []string `json:"delivery_methods"`
}

func parseMembership(raw []byte) (month, error) {
	page := html.UnescapeString(string(raw))
	fields := map[string]string{}
	for _, match := range fieldPattern.FindAllStringSubmatch(page, -1) {
		if fields[match[1]] == "" {
			fields[match[1]] = match[2]
		}
	}
	m := month{Machine: fields["activeContentMachineName"], Product: fields["productHumanName"], End: fields["activeContentEndDate|datetime"]}
	seen := map[string]bool{}
	for _, span := range choiceItem.FindAllStringSubmatchIndex(page, -1) {
		slug := page[span[2]:span[3]]
		if seen[slug] {
			continue
		}
		start := span[0] + strings.IndexByte(page[span[0]:span[1]], '{')
		var item choiceItemData
		if json.NewDecoder(strings.NewReader(page[start:])).Decode(&item) != nil || strings.TrimSpace(item.Title) == "" {
			continue
		}
		seen[slug] = true
		for _, delivery := range item.Delivery {
			if delivery == "steam" {
				m.Titles = append(m.Titles, strings.TrimSpace(item.Title))
				break
			}
		}
	}
	if m.Machine == "" || len(m.Titles) == 0 {
		return month{}, errors.New("页面无当月包 Steam 游戏数据")
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
	for _, match := range [][]string{productMonth.FindStringSubmatch(m.Product), monthKey.FindStringSubmatch(m.Machine)} {
		if len(match) > 0 {
			if mon := parseMonth(match[1]); mon != 0 {
				return fmt.Sprintf("HB慈善包%s年%d月包", match[2][2:], mon)
			}
		}
	}
	return "HB慈善包"
}

func monthURL(machine string) string {
	if match := monthKey.FindStringSubmatch(machine); len(match) > 0 {
		if mon := parseMonth(match[1]); mon != 0 {
			return membershipURL + "/" + mon.String() + "-" + match[2]
		}
	}
	return membershipURL
}

func publicClient() *http.Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = func(r *http.Request) (*url.URL, error) {
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
	r.Header.Set("Accept", "application/json, text/html;q=0.9, */*;q=0.8")
	r.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
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
		fetch, cancel := context.WithTimeout(ctx, 15*time.Second)
		raw, err := s.request(fetch, address, 1<<20)
		cancel()
		if err != nil {
			return 0
		}
		var result struct{ Items []searchHit }
		if json.Unmarshal(raw, &result) != nil {
			return 0
		}
		if len(result.Items) > 0 {
			return pickAppID(title, result.Items, exactOnly)
		}
	}
	return 0
}
