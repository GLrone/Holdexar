package rates

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"math"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/store"
)

const historySource = "frankfurter.v2"

func (s *Service) fetchHistory(ctx context.Context, window gapWindow) (map[string]map[string]float64, error) {
	quotes := []string{}
	for code := range allowedCurrencies {
		if code != "USD" {
			quotes = append(quotes, code)
		}
	}
	sort.Strings(quotes)
	params := url.Values{"from": {window.Start}, "to": {window.End}, "base": {"USD"}, "quotes": {strings.Join(quotes, ",")}}
	data, err := s.opts.Fetch(ctx, HistoryURL+"?"+params.Encode(), 30*time.Second)
	if err != nil {
		return nil, err
	}
	var items []any
	if err = json.Unmarshal(data, &items); err != nil {
		return nil, fmt.Errorf("历史汇率响应无效：%w", err)
	}
	if items == nil {
		return nil, fmt.Errorf("历史汇率响应不是列表")
	}
	raw := map[string]map[string]float64{}
	for _, rawItem := range items {
		item, ok := rawItem.(map[string]any)
		if !ok {
			continue
		}
		day, _ := item["date"].(string)
		if len(day) > 10 {
			day = day[:10]
		}
		if _, err = time.Parse("2006-01-02", day); err != nil {
			continue
		}
		code, _ := item["quote"].(string)
		code = strings.ToUpper(strings.TrimSpace(code))
		if code == "" {
			continue
		}
		var rate float64
		switch v := item["rate"].(type) {
		case float64:
			rate = v
		case string:
			rate, err = strconv.ParseFloat(v, 64)
			if err != nil {
				continue
			}
		default:
			continue
		}
		if rate <= 0 || math.IsNaN(rate) || math.IsInf(rate, 0) {
			continue
		}
		if raw[day] == nil {
			raw[day] = map[string]float64{}
		}
		raw[day][code] = rate
	}
	result := map[string]map[string]float64{}
	for day, quotes := range raw {
		cny := quotes["CNY"]
		if cny <= 0 {
			continue
		}
		row := map[string]float64{"CNY": 1, "USD": cny}
		for code, rate := range quotes {
			if code != "CNY" && code != "USD" && allowedCurrencies[code] {
				row[code] = cny / rate
			}
		}
		result[day] = row
	}
	return result, nil
}

type historyApplied struct {
	Written  int
	Touched  map[string][]string
	Revalued map[string]int
}

func (s *Service) writeHistory(ctx context.Context, data map[string]map[string]float64) (historyApplied, error) {
	values := []map[string]any{}
	days := []string{}
	for day := range data {
		days = append(days, day)
	}
	sort.Strings(days)
	for _, day := range days {
		codes := []string{}
		for code := range data[day] {
			codes = append(codes, code)
		}
		sort.Strings(codes)
		for _, code := range codes {
			if allowedCurrencies[code] {
				values = append(values, map[string]any{"date": day, "code": code, "rate": data[day][code]})
			}
		}
	}
	applied := historyApplied{Touched: map[string][]string{}}
	if len(values) == 0 {
		return applied, nil
	}
	err := s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		rows, err := conn.QueryContext(ctx, `INSERT INTO fx_rate_history(currency_code,rate_date,rate_to_cny,source,source_kind,fetched_at,observed_at)
 SELECT json_extract(value,'$.code'),json_extract(value,'$.date'),json_extract(value,'$.rate'),?,'derived',?,NULL FROM json_each(?) WHERE 1
 ON CONFLICT(currency_code,rate_date) DO UPDATE SET rate_to_cny=excluded.rate_to_cny,source=excluded.source,source_kind=excluded.source_kind,fetched_at=excluded.fetched_at,observed_at=NULL
 WHERE (LOWER(COALESCE(fx_rate_history.source_kind,''))!='observed' OR LOWER(COALESCE(fx_rate_history.source,''))='bing.currencyapi') AND LOWER(COALESCE(fx_rate_history.source,''))!=?
 RETURNING currency_code,CAST(rate_date AS TEXT)`, historySource, s.now().Format("2006-01-02 15:04:05.000000"), fxJSON(values), historySource)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var code, day string
			if err = rows.Scan(&code, &day); err != nil {
				return err
			}
			applied.Touched[code] = append(applied.Touched[code], day)
			applied.Written++
		}
		if err = rows.Err(); err != nil {
			return err
		}
		if err = rows.Close(); err != nil {
			return err
		}
		if applied.Written > 0 && s.opts.Revalue != nil {
			applied.Revalued, err = s.opts.Revalue(ctx, conn, applied.Touched)
		}
		return err
	})
	if err != nil {
		return historyApplied{}, err
	}
	return applied, nil
}

func (s *Service) Repair(ctx context.Context, dryRun bool, maxWindows *int) (map[string]any, error) {
	scan, err := scanHistoryGaps(ctx, s.DB, s.now())
	if err != nil {
		return nil, err
	}
	windows := []map[string]any{}
	result := map[string]any{"status": "no_gaps", "windowsPlanned": len(scan.Windows), "windows": windows, "requests": 0, "written": 0, "currencies": scan.Currencies, "totalPairs": scan.TotalPairs}
	if len(scan.Windows) == 0 {
		return result, nil
	}
	planned := scan.Windows
	if maxWindows != nil {
		planned = planned[:min(len(planned), max(0, *maxWindows))]
	}
	if dryRun {
		result["status"] = "dry_run"
		result["planned"] = planned
		return result, nil
	}
	work, cancel := context.WithCancel(ctx)
	defer cancel()
	type fetched struct {
		data map[string]map[string]float64
		err  error
	}
	pending := make([]chan fetched, len(planned))
	for i, window := range planned {
		pending[i] = make(chan fetched, 1)
		go func(ch chan fetched, w gapWindow) { data, e := s.fetchHistory(work, w); ch <- fetched{data, e} }(pending[i], window)
	}
	combined := map[string]map[string]float64{}
	parts := []map[string]map[string]float64{}
	requests := 0
	result["status"] = "ok"
	for i, window := range planned {
		var part fetched
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case part = <-pending[i]:
		}
		if part.err != nil {
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			message := []rune(part.err.Error())
			if len(message) > 200 {
				message = message[:200]
			}
			result["status"] = "provider_error"
			result["error"] = string(message)
			cancel()
			break
		}
		requests++
		parts = append(parts, part.data)
		windows = append(windows, map[string]any{"start": window.Start, "end": window.End, "written": 0, "days": len(part.data)})
		for day, values := range part.data {
			if combined[day] == nil {
				combined[day] = map[string]float64{}
			}
			for code, rate := range values {
				combined[day][code] = rate
			}
		}
	}
	applied, err := s.writeHistory(ctx, combined)
	if err != nil {
		return nil, err
	}
	eligible := map[string]bool{}
	for code, days := range applied.Touched {
		for _, day := range days {
			eligible[code+":"+day] = true
		}
	}
	for i, data := range parts {
		count := 0
		for day, values := range data {
			for code := range values {
				key := code + ":" + day
				if eligible[key] {
					count++
					delete(eligible, key)
				}
			}
		}
		windows[i]["written"] = count
	}
	result["requests"], result["written"], result["windows"] = requests, applied.Written, windows
	if applied.Revalued != nil {
		result["revalued"] = applied.Revalued
	}
	s.opts.Log("历史汇率补全结束 │ 状态=%s，完成请求=%d，写入=%d", result["status"], requests, applied.Written)
	return result, nil
}
