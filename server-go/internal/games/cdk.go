package games

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"sync"
	"time"
)

// 第三方 CDK 查价：只读库内标准版 sub_id，进程内 TTL 缓存（256 FIFO，错误短 TTL + 单飞 SWR）。

var (
	cdkSteampyURL       = "https://steampy.com/xboot/common/plugIn/getGame"
	cdkSteampyGameURL   = "https://steampy.com/game/%d"
	cdkSteampyDetailURL = "https://steampy.com/cdkDetail?name=cn&gameId=%s"
	cdkCiciURL          = "https://steamcici.com/prod-api/user/system/shopGame/list"
	cdkCiciHomeURL      = "https://www.steamcici.com/"
)

const (
	cdkTimeout  = 15 * time.Second
	cdkTTL      = 300 * time.Second
	cdkErrorTTL = 60 * time.Second
	cdkCacheMax = 256
)

type cdkPlatformResult struct {
	Listed bool    `json:"listed"`
	Price  *string `json:"price"`
	URL    string  `json:"url"`
	Error  string  `json:"error,omitempty"`
}

type cdkResult struct {
	AppID     int64             `json:"appid"`
	SubID     *int64            `json:"subId"`
	Steampy   cdkPlatformResult `json:"steampy"`
	Steamcici cdkPlatformResult `json:"steamcici"`
}

type cdkCacheKey struct {
	appid  int64
	sub    int64
	hasSub bool
}

type cdkEntry struct {
	at   time.Time
	data cdkResult
}

type cdkFetcher struct {
	db     *sql.DB
	proxy  func(context.Context) (string, error)
	client *http.Client

	mu         sync.Mutex
	cache      map[cdkCacheKey]cdkEntry
	order      []cdkCacheKey
	refreshing map[cdkCacheKey]bool
}

func newCDKFetcher(db *sql.DB, proxy func(context.Context) (string, error)) *cdkFetcher {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	return &cdkFetcher{
		db: db, proxy: proxy,
		client:     &http.Client{Transport: transport, Timeout: cdkTimeout},
		cache:      map[cdkCacheKey]cdkEntry{},
		refreshing: map[cdkCacheKey]bool{},
	}
}

func (h *handler) handleCDK(w http.ResponseWriter, r *http.Request) {
	appid, err := parseAppID(r.PathValue("appid"))
	if err != nil {
		writeJSON(w, http.StatusUnprocessableEntity, intParseDetail(r.PathValue("appid")))
		return
	}
	var subID *int64
	if raw := r.URL.Query().Get("sub_id"); raw != "" {
		v, e := parseAppID(raw)
		if e != nil {
			writeJSON(w, http.StatusUnprocessableEntity, queryError("sub_id", errIntParsing, errIntParsingMsg, raw))
			return
		}
		subID = &v
	}
	result, err := h.cdk.fetch(r.Context(), appid, subID)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, detailNull())
		return
	}
	writeJSON(w, http.StatusOK, result)
}

// resolveSubID 取 CN 优先、回退任意区最小 sub_id（missing 行同样有效）。
func (f *cdkFetcher) resolveSubID(ctx context.Context, appid int64) (*int64, error) {
	var sub sql.NullInt64
	err := f.db.QueryRowContext(ctx,
		`SELECT sub_id FROM game_current_prices
		 WHERE appid=? AND sub_id IS NOT NULL AND sub_id > 0
		 ORDER BY (region_code = 'CN') DESC, sub_id ASC LIMIT 1`, appid).Scan(&sub)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	v := sub.Int64
	return &v, nil
}

func (f *cdkFetcher) fetch(ctx context.Context, appid int64, subID *int64) (cdkResult, error) {
	key := cdkCacheKey{appid: appid}
	if subID != nil {
		key.hasSub, key.sub = true, *subID
	}
	now := time.Now()
	f.mu.Lock()
	if entry, ok := f.cache[key]; ok {
		ttl := cdkTTL
		if cdkHadError(entry.data) {
			ttl = cdkErrorTTL
		}
		if now.Sub(entry.at) < ttl {
			f.mu.Unlock()
			return entry.data, nil
		}
		if !f.refreshing[key] {
			f.refreshing[key] = true
			bgCtx := context.Background()
			go func() {
				fresh, err := f.fetchUncached(bgCtx, appid, subID)
				f.mu.Lock()
				defer f.mu.Unlock()
				if err == nil {
					// 刷新期间键可能已被 FIFO 淘汰：写回时补齐淘汰序。
					if _, exists := f.cache[key]; !exists {
						f.order = append(f.order, key)
					}
					f.cache[key] = cdkEntry{at: time.Now(), data: fresh}
				} else if _, ok := f.cache[key]; ok {
					f.cache[key] = cdkEntry{at: time.Now(), data: f.cache[key].data}
				}
				delete(f.refreshing, key)
			}()
		}
		f.mu.Unlock()
		return entry.data, nil
	}
	f.mu.Unlock()

	fresh, err := f.fetchUncached(ctx, appid, subID)
	if err != nil {
		return cdkResult{}, err
	}
	f.mu.Lock()
	if len(f.cache) >= cdkCacheMax {
		delete(f.cache, f.order[0])
		f.order = f.order[1:]
	}
	f.cache[key] = cdkEntry{at: now, data: fresh}
	f.order = append(f.order, key)
	f.mu.Unlock()
	return fresh, nil
}

func (f *cdkFetcher) fetchUncached(ctx context.Context, appid int64, subID *int64) (cdkResult, error) {
	if subID == nil {
		resolved, err := f.resolveSubID(ctx, appid)
		if err != nil {
			return cdkResult{}, err
		}
		subID = resolved
	}
	proxyURL := ""
	if f.proxy != nil {
		if v, err := f.proxy(ctx); err == nil {
			proxyURL = v
		}
	}
	var steampy, cici cdkPlatformResult
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		steampy = f.fetchSteampy(ctx, appid, subID, proxyURL)
	}()
	go func() {
		defer wg.Done()
		cici = f.fetchSteamcici(ctx, appid, subID, proxyURL)
	}()
	wg.Wait()
	return cdkResult{AppID: appid, SubID: subID, Steampy: steampy, Steamcici: cici}, nil
}

func cdkTruthy(v any) bool {
	switch t := v.(type) {
	case nil:
		return false
	case bool:
		return t
	case json.Number:
		f, err := t.Float64()
		return err == nil && f != 0
	case float64:
		return t != 0
	case string:
		return t != ""
	}
	return true
}

func cdkDecodeJSON(body []byte, out any) error {
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber()
	return dec.Decode(out)
}

func cdkScalar(v any) string {
	switch t := v.(type) {
	case json.Number:
		return t.String()
	case string:
		return t
	case bool:
		if t {
			return "True"
		}
		return "False"
	case nil:
		return "None"
	}
	return fmt.Sprint(v)
}

func (f *cdkFetcher) httpGet(ctx context.Context, rawURL, proxyURL, referer string) ([]byte, error) {
	client := f.client
	if proxyURL != "" {
		u, err := url.Parse(proxyURL)
		if err != nil {
			return nil, err
		}
		base := f.client.Transport
		if base == nil {
			base = http.DefaultTransport
		}
		transport, ok := base.(*http.Transport)
		if !ok {
			return nil, fmt.Errorf("CDK 代理需要 HTTP transport")
		}
		tr := transport.Clone()
		tr.Proxy = http.ProxyURL(u)
		defer tr.CloseIdleConnections()
		clone := *f.client
		clone.Transport = tr
		client = &clone
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", "Mozilla/5.0")
	req.Header.Set("Referer", referer)
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	return io.ReadAll(resp.Body)
}

func (f *cdkFetcher) fetchSteampy(ctx context.Context, appid int64, subID *int64, proxyURL string) cdkPlatformResult {
	empty := cdkPlatformResult{
		URL: fmt.Sprintf(cdkSteampyGameURL, appid),
	}
	if subID == nil || *subID == 0 {
		return empty
	}
	q := url.Values{}
	q.Set("subId", strconv.FormatInt(*subID, 10))
	q.Set("appId", strconv.FormatInt(appid, 10))
	q.Set("type", "subid")
	body, err := f.httpGet(ctx, cdkSteampyURL+"?"+q.Encode(), proxyURL, "https://steampy.com/")
	if err != nil {
		empty.Error = err.Error()
		return empty
	}
	var data map[string]any
	if err := cdkDecodeJSON(body, &data); err != nil {
		empty.Error = err.Error()
		return empty
	}
	result, _ := data["result"].(map[string]any)
	if !cdkTruthy(data["success"]) || len(result) == 0 {
		return empty
	}
	keyPrice := result["keyPrice"]
	gameID := result["id"]
	out := cdkPlatformResult{Listed: cdkTruthy(keyPrice), URL: empty.URL}
	if cdkTruthy(keyPrice) {
		price := "¥" + cdkScalar(keyPrice)
		out.Price = &price
	}
	if cdkTruthy(gameID) {
		out.URL = fmt.Sprintf(cdkSteampyDetailURL, cdkScalar(gameID))
	}
	return out
}

func (f *cdkFetcher) fetchSteamcici(ctx context.Context, appid int64, subID *int64, proxyURL string) cdkPlatformResult {
	empty := cdkPlatformResult{URL: cdkCiciHomeURL}
	if subID == nil || *subID == 0 {
		return empty
	}
	q := url.Values{}
	q.Set("parentId", strconv.FormatInt(appid, 10))
	body, err := f.httpGet(ctx, cdkCiciURL+"?"+q.Encode(), proxyURL, "https://www.steamcici.com/")
	if err != nil {
		empty.Error = err.Error()
		return empty
	}
	var data struct {
		Code any              `json:"code"`
		Rows []map[string]any `json:"rows"`
	}
	if err := cdkDecodeJSON(body, &data); err != nil {
		empty.Error = err.Error()
		return empty
	}
	codeF, _ := toFloat(data.Code)
	if codeF != 200 || len(data.Rows) == 0 {
		return empty
	}
	want := strconv.FormatInt(*subID, 10)
	for _, row := range data.Rows {
		if cdkScalar(row["gameId"]) != want {
			continue
		}
		if price, ok := row["lastLowSellPrice"]; ok && price != nil {
			v := "¥" + cdkScalar(price)
			return cdkPlatformResult{Listed: true, Price: &v, URL: cdkCiciHomeURL}
		}
		return empty
	}
	return empty
}

func toFloat(v any) (float64, bool) {
	switch t := v.(type) {
	case float64:
		return t, true
	case json.Number:
		f, err := t.Float64()
		return f, err == nil
	}
	return 0, false
}

func cdkHadError(data cdkResult) bool {
	return data.Steampy.Error != "" || data.Steamcici.Error != ""
}
