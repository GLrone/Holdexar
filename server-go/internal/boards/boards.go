package boards

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"sync"
	"time"
)

const SearchURL = "https://store.steampowered.com/search/results"
const HotTTL = time.Hour
const StaleTTL = 24 * time.Hour
const BatchSize = 100
const BatchInterval = 500 * time.Millisecond

type Options struct {
	Endpoint   string
	HTTPClient *http.Client
	Proxy      func(context.Context) (string, error)
	Now        func() time.Time
	Sleep      func(context.Context, time.Duration) error
	Log        func(string, ...any)
}

type board struct {
	params                                  url.Values
	maxRecords, maxRequests, emptyTolerance int
}

var definitions = map[string]board{
	"topsellers": {url.Values{"filter": {"topsellers"}, "hidef2p": {"1"}, "category1": {"998"}}, 500, 5, 1},
	"popularnew": {url.Values{"filter": {"popularnew"}, "sort_by": {"Released_DESC"}, "hidef2p": {"1"}, "category1": {"998"}}, 500, 5, 1},
	"specials":   {url.Values{"specials": {"1"}, "sort_by": {"Global_Topsellers"}, "hidef2p": {"1"}, "category1": {"998"}, "l": {"english"}}, 5000, 50, 3},
	"comingsoon": {url.Values{"filter": {"popularcomingsoon"}, "os": {"win"}}, 500, 5, 1},
}

var logoAppID = regexp.MustCompile(`/apps/(\d+)/`)

type snapshot struct {
	mu  sync.Mutex
	ids []int64
	at  time.Time
}

type Client struct {
	opts   Options
	states map[string]*snapshot
}

func New(opts Options) *Client {
	if opts.Endpoint == "" {
		opts.Endpoint = SearchURL
	}
	if opts.HTTPClient == nil {
		transport := http.DefaultTransport.(*http.Transport).Clone()
		transport.Proxy = nil
		opts.HTTPClient = &http.Client{Transport: transport, Timeout: 15 * time.Second}
	}
	if opts.Now == nil {
		opts.Now = time.Now
	}
	if opts.Sleep == nil {
		opts.Sleep = func(ctx context.Context, d time.Duration) error {
			timer := time.NewTimer(d)
			defer timer.Stop()
			select {
			case <-timer.C:
				return nil
			case <-ctx.Done():
				return ctx.Err()
			}
		}
	}
	if opts.Log == nil {
		opts.Log = func(string, ...any) {}
	}
	c := &Client{opts: opts, states: map[string]*snapshot{}}
	for key := range definitions {
		c.states[key] = &snapshot{}
	}
	return c
}

func (c *Client) Get(ctx context.Context, key string) ([]int64, error) { return c.get(ctx, key, false) }
func (c *Client) Refresh(ctx context.Context, key string) ([]int64, error) {
	return c.get(ctx, key, true)
}

func (c *Client) get(ctx context.Context, key string, refresh bool) ([]int64, error) {
	state, ok := c.states[key]
	if !ok {
		return nil, fmt.Errorf("未知榜单源: %s", key)
	}
	state.mu.Lock()
	defer state.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if !refresh && state.ids != nil && c.opts.Now().Sub(state.at) < HotTTL {
		return append([]int64{}, state.ids...), nil
	}
	ids, err := c.fetch(ctx, key)
	if err != nil && ctx.Err() != nil {
		return nil, ctx.Err()
	}
	if err != nil {
		c.opts.Log("榜单 %s 获取失败：%v", key, err)
	}
	if len(ids) > 0 {
		state.ids = append([]int64{}, ids...)
		state.at = c.opts.Now()
		return ids, nil
	}
	if state.ids != nil && (refresh || c.opts.Now().Sub(state.at) < StaleTTL) {
		return append([]int64{}, state.ids...), nil
	}
	return []int64{}, nil
}

func (c *Client) fetch(ctx context.Context, key string) ([]int64, error) {
	def := definitions[key]
	collected := []int64{}
	seen := map[int64]bool{}
	empty := 0
	client := c.opts.HTTPClient
	if c.opts.Proxy != nil {
		proxy, err := c.opts.Proxy(ctx)
		if err != nil {
			if ctx.Err() != nil {
				return collected, ctx.Err()
			}
			c.opts.Log("榜单代理解析失败，使用直连：%v", err)
		}
		if err == nil && proxy != "" {
			u, err := url.Parse(proxy)
			if err != nil {
				return collected, err
			}
			base := client.Transport
			if base == nil {
				base = http.DefaultTransport
			}
			transport, ok := base.(*http.Transport)
			if !ok {
				return collected, fmt.Errorf("榜单代理需要 HTTP transport")
			}
			tr := transport.Clone()
			tr.Proxy = http.ProxyURL(u)
			defer tr.CloseIdleConnections()
			clone := *client
			clone.Transport = tr
			client = &clone
		}
	}
	for page := 0; page < def.maxRequests && len(collected) < def.maxRecords && empty < def.emptyTolerance; page++ {
		endpoint, err := url.Parse(c.opts.Endpoint)
		if err != nil {
			return collected, err
		}
		query := endpoint.Query()
		query.Set("start", strconv.Itoa(page*BatchSize))
		query.Set("count", strconv.Itoa(BatchSize))
		query.Set("json", "1")
		for k, values := range def.params {
			query[k] = values
		}
		endpoint.RawQuery = query.Encode()
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint.String(), nil)
		if err != nil {
			return collected, err
		}
		req.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
		req.Header.Set("Accept", "application/json")
		req.Header.Set("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
		req.Header.Set("Cookie", "birthtime=315532800; mature_content=1")
		resp, err := client.Do(req)
		if err != nil {
			return collected, err
		}
		if resp.StatusCode != http.StatusOK {
			resp.Body.Close()
			return collected, fmt.Errorf("榜单 %s HTTP %d", key, resp.StatusCode)
		}
		var data struct {
			Items []struct {
				Logo string `json:"logo"`
			} `json:"items"`
		}
		err = json.NewDecoder(resp.Body).Decode(&data)
		resp.Body.Close()
		if err != nil {
			return collected, err
		}
		before := len(collected)
		for _, item := range data.Items {
			match := logoAppID.FindStringSubmatch(item.Logo)
			if len(match) < 2 {
				continue
			}
			id, err := strconv.ParseInt(match[1], 10, 64)
			if err != nil || seen[id] {
				continue
			}
			seen[id] = true
			collected = append(collected, id)
			if len(collected) >= def.maxRecords {
				break
			}
		}
		if len(collected) == before {
			empty++
		} else {
			empty = 0
		}
		if len(collected) < def.maxRecords {
			if err = c.opts.Sleep(ctx, BatchInterval); err != nil {
				return collected, err
			}
		}
	}
	return collected, nil
}
