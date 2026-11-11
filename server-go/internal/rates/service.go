package rates

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"sync"
	"time"

	"holdexar/server-go/internal/bundles"
	"holdexar/server-go/internal/derived"
	"holdexar/server-go/internal/jobrun"
	"holdexar/server-go/internal/netenv"
	"holdexar/server-go/internal/store"
)

const AugmentedURL = "https://api.augmentedsteam.com/rates/v1?to=CNY"
const ExchangeURL = "https://open.er-api.com/v6/latest/CNY"
const HistoryURL = "https://api.frankfurter.dev/v2/rates"

var ErrSourcesUnavailable = errors.New("所有汇率源均失败")

type ServiceOptions struct {
	Now      func() time.Time
	Fetch    func(context.Context, string, time.Duration) ([]byte, error)
	Log      func(string, ...any)
	Revalue  func(context.Context, *sql.Conn, map[string][]string) (map[string]int, error)
	Recorder *jobrun.Recorder
}
type refreshCall struct {
	done   chan struct{}
	result map[string]any
	err    error
}
type Service struct {
	DB         *sql.DB
	Gate       *store.WriteGate
	opts       ServiceOptions
	mu         sync.Mutex
	refreshing *refreshCall
	closing    bool
	jobs       sync.WaitGroup
}

func NewService(db *sql.DB, gate *store.WriteGate, opts ServiceOptions) *Service {
	if opts.Now == nil {
		opts.Now = time.Now
	}
	if opts.Fetch == nil {
		opts.Fetch = fetchJSON
	}
	if opts.Log == nil {
		opts.Log = func(string, ...any) {}
	}
	return &Service{DB: db, Gate: gate, opts: opts}
}

func fetchJSON(ctx context.Context, address string, timeout time.Duration) ([]byte, error) {
	proxy, e := netenv.ResolveSystemProxy(ctx)
	if e != nil {
		return nil, e
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	if proxy != "" {
		parsed, e := url.Parse(proxy)
		if e != nil {
			return nil, e
		}
		transport.Proxy = http.ProxyURL(parsed)
	}
	defer transport.CloseIdleConnections()
	client := &http.Client{Timeout: timeout, Transport: transport}
	req, e := http.NewRequestWithContext(ctx, http.MethodGet, address, nil)
	if e != nil {
		return nil, e
	}
	res, e := client.Do(req)
	if e != nil {
		return nil, e
	}
	defer res.Body.Close()
	if res.StatusCode < 200 || res.StatusCode >= 300 {
		return nil, fmt.Errorf("汇率来源返回HTTP %d", res.StatusCode)
	}
	data, e := io.ReadAll(io.LimitReader(res.Body, (64<<20)+1))
	if e != nil {
		return nil, e
	}
	if len(data) > 64<<20 {
		return nil, errors.New("汇率响应过大")
	}
	return data, nil
}
func (s *Service) now() time.Time { return s.opts.Now().In(time.FixedZone("Beijing", 8*3600)) }
func fxJSON(v any) string         { b, _ := json.Marshal(v); return string(b) }

func (s *Service) fetchCurrent(ctx context.Context, address string, reciprocal bool) (map[string]float64, error) {
	var data []byte
	var err error
	for attempt := 0; attempt < 2; attempt++ {
		data, err = s.opts.Fetch(ctx, address, 15*time.Second)
		if err == nil {
			break
		}
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
	}
	if err != nil {
		return nil, err
	}
	var values map[string]any
	if err = json.Unmarshal(data, &values); err != nil {
		return nil, err
	}
	if reciprocal {
		var ok bool
		values, ok = values["rates"].(map[string]any)
		if !ok || len(values) == 0 {
			return nil, errors.New("汇率响应缺少报价")
		}
	}
	rates := map[string]float64{}
	for code, value := range values {
		if row, ok := value.(map[string]any); ok {
			value = row["CNY"]
		}
		if number, ok := value.(float64); ok {
			if reciprocal {
				if number == 0 {
					continue
				}
				number = 1 / number
			}
			rates[strings.ToUpper(code)] = number
		}
	}
	if !reciprocal && len(rates) < 5 {
		return nil, errors.New("汇率响应币种不足")
	}
	return rates, nil
}

func (s *Service) Refresh(ctx context.Context) (map[string]any, error) {
	s.mu.Lock()
	if inFlight := s.refreshing; inFlight != nil {
		s.mu.Unlock()
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-inFlight.done:
			return inFlight.result, inFlight.err
		}
	}
	call := &refreshCall{done: make(chan struct{})}
	s.refreshing = call
	s.mu.Unlock()
	defer func() {
		failure := recover()
		if failure != nil {
			call.err = errors.New("汇率刷新异常结束")
		}
		s.mu.Lock()
		s.refreshing = nil
		close(call.done)
		s.mu.Unlock()
		if failure != nil {
			panic(failure)
		}
	}()
	call.result, call.err = s.refresh(ctx)
	return call.result, call.err
}

func (s *Service) refresh(ctx context.Context) (map[string]any, error) {
	values, err := s.fetchCurrent(ctx, AugmentedURL, false)
	source := "augmentedsteam"
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		s.opts.Log("首选汇率来源不可用，正在使用备用来源：%v", err)
		values, err = s.fetchCurrent(ctx, ExchangeURL, true)
		source = "er-api"
	}
	if err != nil {
		return nil, ErrSourcesUnavailable
	}
	values["CNY"] = 1
	for code := range values {
		if !allowedCurrencies[code] {
			delete(values, code)
		}
	}
	now := s.now()
	stamp := now.Format("2006-01-02 15:04:05.000000")
	date := now.Format("2006-01-02")
	rows := make([]map[string]any, 0, len(values))
	codes := []string{}
	for code := range values {
		codes = append(codes, code)
	}
	sort.Strings(codes)
	for _, code := range codes {
		rows = append(rows, map[string]any{"code": code, "rate": values[code]})
	}
	var recomputed map[string]int64
	err = s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		if _, e := conn.ExecContext(ctx, `INSERT INTO fx_rates(currency_code,rate_to_cny,fetched_at) SELECT json_extract(value,'$.code'),json_extract(value,'$.rate'),? FROM json_each(?) WHERE 1 ON CONFLICT(currency_code) DO UPDATE SET rate_to_cny=excluded.rate_to_cny,fetched_at=excluded.fetched_at`, stamp, fxJSON(rows)); e != nil {
			return e
		}
		if _, e := conn.ExecContext(ctx, `INSERT INTO fx_rate_history(currency_code,rate_to_cny,source,source_kind,rate_date,fetched_at) SELECT json_extract(value,'$.code'),json_extract(value,'$.rate'),?,'observed',?,? FROM json_each(?) WHERE 1 ON CONFLICT(currency_code,rate_date) DO UPDATE SET rate_to_cny=excluded.rate_to_cny,source=excluded.source,source_kind='observed',fetched_at=excluded.fetched_at`, source, date, stamp, fxJSON(rows)); e != nil {
			return e
		}
		allowed := []string{}
		for code := range allowedCurrencies {
			allowed = append(allowed, code)
		}
		for _, table := range []string{"fx_rates", "fx_rate_history"} {
			if _, e := conn.ExecContext(ctx, "DELETE FROM "+table+" WHERE currency_code NOT IN(SELECT value FROM json_each(?))", fxJSON(allowed)); e != nil {
				return e
			}
		}
		var e error
		recomputed, e = recomputeCNY(ctx, conn)
		if e != nil {
			return e
		}
		if _, e = derived.UpdatePriceDerived(ctx, conn, nil, derived.Options{Now: now, History: false}); e != nil {
			return e
		}
		_, e = bundles.RefreshSortCache(ctx, conn, nil, nil)
		return e
	})
	if err != nil {
		return nil, err
	}
	s.opts.Log("汇率刷新完成 │ 来源=%s，币种=%d，重算游戏=%d，重算捆绑包=%d", source, len(values), recomputed["games"], recomputed["bundles"])
	return map[string]any{"source": source, "count": len(values), "recomputed": recomputed, "fetchedAt": isoTimestamp(now)}, nil
}
func isoTimestamp(now time.Time) string {
	if now.Nanosecond() == 0 {
		return now.Format("2006-01-02T15:04:05")
	}
	return now.Format("2006-01-02T15:04:05.000000")
}

func (s *Service) enabled(ctx context.Context, key string) (bool, error) {
	var raw string
	err := s.DB.QueryRowContext(ctx, "SELECT value_json FROM app_settings WHERE key=?", key).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return true, nil
	}
	if err != nil {
		return false, err
	}
	var value any
	if err = json.Unmarshal([]byte(raw), &value); err != nil {
		return false, err
	}
	switch v := value.(type) {
	case nil:
		return false, nil
	case bool:
		return v, nil
	case float64:
		return v != 0, nil
	case string:
		return v != "", nil
	case []any:
		return len(v) > 0, nil
	case map[string]any:
		return len(v) > 0, nil
	}
	return true, nil
}
func (s *Service) RefreshIfStale(ctx context.Context) (bool, error) {
	on, err := s.enabled(ctx, "crawl.auto_refresh_rates")
	if err != nil || !on {
		return false, err
	}
	var fetched sql.NullString
	if err = s.DB.QueryRowContext(ctx, "SELECT CAST(MAX(fetched_at) AS TEXT) FROM fx_rates WHERE currency_code!='CNY'").Scan(&fetched); err != nil {
		return false, err
	}
	if fetched.Valid {
		for _, layout := range []string{"2006-01-02 15:04:05.999999999", "2006-01-02T15:04:05.999999999", time.RFC3339Nano} {
			if at, e := time.ParseInLocation(layout, fetched.String, s.now().Location()); e == nil {
				if s.now().Sub(at) < 12*time.Hour {
					return false, nil
				}
				break
			}
		}
	}
	_, err = s.Refresh(ctx)
	return err == nil, err
}
