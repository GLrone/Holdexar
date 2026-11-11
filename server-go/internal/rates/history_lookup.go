package rates

import (
	"context"
	"database/sql"
	"sort"
	"strings"
	"time"
)

type HistoryQuery interface {
	QueryContext(context.Context, string, ...any) (*sql.Rows, error)
}
type HistoricalRate struct {
	Date string
	Rate float64
}

func LoadHistoricalRates(ctx context.Context, q HistoryQuery, currencies []string, start, end string) (map[string][]HistoricalRate, error) {
	rows, e := q.QueryContext(ctx, `SELECT UPPER(currency_code),CAST(rate_date AS TEXT),rate_to_cny FROM fx_rate_history WHERE currency_code IN(SELECT value FROM json_each(?)) AND source_kind IN('observed','derived') AND rate_date>=? AND rate_date<=? ORDER BY rate_date`, fxJSON(currencies), start, end)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	out := map[string][]HistoricalRate{}
	for rows.Next() {
		var code string
		var row HistoricalRate
		if e = rows.Scan(&code, &row.Date, &row.Rate); e != nil {
			return nil, e
		}
		out[code] = append(out[code], row)
	}
	return out, rows.Err()
}

func PickHistoricalRate(series []HistoricalRate, currency, day string) *float64 {
	if strings.ToUpper(currency) == "CNY" {
		v := 1.0
		return &v
	}
	i := sort.Search(len(series), func(i int) bool { return series[i].Date > day }) - 1
	if i < 0 {
		return nil
	}
	at, e := time.Parse("2006-01-02", day)
	if e != nil {
		if series[i].Date != day {
			return nil
		}
	} else {
		earliest := at.AddDate(0, 0, -15).Format("2006-01-02")
		if series[i].Date < earliest {
			return nil
		}
	}
	v := series[i].Rate
	return &v
}
