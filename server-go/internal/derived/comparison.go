package derived

import (
	"context"
	"database/sql"
	_ "embed"
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

//go:embed comparison_query.sql
var comparisonSQL string

type ComparisonQuery interface {
	QueryContext(context.Context, string, ...any) (*sql.Rows, error)
}

func ComparisonKey(current map[string]any) string {
	b, _ := json.Marshal([]any{current["appid"], strings.ToUpper(fmt.Sprint(current["region"])), current["subId"], strings.ToUpper(fmt.Sprint(current["currency"]))})
	return string(b)
}

func comparisonTime(raw string) (time.Time, error) {
	for _, layout := range []string{"2006-01-02T15:04:05.999999999", "2006-01-02 15:04:05.999999999", time.RFC3339Nano} {
		if t, e := time.Parse(layout, raw); e == nil {
			return t, nil
		}
	}
	return time.Time{}, fmt.Errorf("invalid observation timestamp")
}
func comparisonISO(v any) any {
	if s, ok := v.(string); ok {
		if t, e := comparisonTime(s); e == nil {
			return t.Format("2006-01-02T15:04:05.999999")
		}
	}
	return v
}

func HistoryContext(ctx context.Context, q ComparisonQuery, currents []map[string]any, before time.Time, window *int) (map[string]map[string]any, error) {
	if window != nil && *window != 90 && *window != 180 && *window != 365 {
		return nil, fmt.Errorf("window_days must be 90, 180, 365 or None")
	}
	wanted := map[string]map[string]any{}
	for _, c := range currents {
		if c["appid"] == nil || c["subId"] == nil || fmt.Sprint(c["subId"]) == "0" || c["currency"] == nil || c["region"] == nil {
			continue
		}
		at := before
		if s, ok := c["observedAt"].(string); ok && s != "" {
			var e error
			at, e = comparisonTime(s)
			if e != nil {
				return nil, e
			}
		}
		var low any
		if window != nil {
			low = at.AddDate(0, 0, -*window).Format("2006-01-02 15:04:05.000000")
		}
		wanted[ComparisonKey(c)] = map[string]any{"a": c["appid"], "r": strings.ToUpper(fmt.Sprint(c["region"])), "s": c["subId"], "c": strings.ToUpper(fmt.Sprint(c["currency"])), "b": at.Format("2006-01-02 15:04:05.000000"), "lo": low, "p": c["price"]}
	}
	result := map[string]map[string]any{}
	if len(wanted) == 0 {
		return result, nil
	}
	units := []map[string]any{}
	for _, u := range wanted {
		units = append(units, u)
	}
	raw, e := json.Marshal(units)
	if e != nil {
		return nil, e
	}
	rows, e := q.QueryContext(ctx, comparisonSQL, string(raw))
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	for rows.Next() {
		values := make([]any, 16)
		ptrs := make([]any, 16)
		for i := range values {
			ptrs[i] = &values[i]
		}
		if e = rows.Scan(ptrs...); e != nil {
			return nil, e
		}
		for i, v := range values {
			if b, ok := v.([]byte); ok {
				values[i] = string(b)
			}
		}
		h := map[string]any{"baselineLow": values[4], "points": values[5], "firstAt": comparisonISO(values[6]), "lastAt": comparisonISO(values[7]), "previousPrice": values[8], "previousOriginalPrice": values[9], "previousCnyFen": values[10], "previousObservedAt": comparisonISO(values[11]), "previousSource": values[12], "previousRowId": values[13], "complete": false, "currentKnown": values[14] == int64(1), "observationId": nil}
		if h["currentKnown"] == true {
			h["observationId"] = values[15]
		}
		result[ComparisonKey(map[string]any{"appid": values[0], "region": values[1], "subId": values[2], "currency": values[3]})] = h
	}
	return result, rows.Err()
}
