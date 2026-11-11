package bills

import (
	"context"
	"database/sql"
	"encoding/json"
	"sort"
	"strings"
	"time"

	"holdexar/server-go/internal/rates"
)

func RevalueAffected(ctx context.Context, conn *sql.Conn, touched map[string][]string) (map[string]int, error) {
	result := map[string]int{"bills": 0, "transactions": 0}
	bounds := []map[string]string{}
	codes := []string{}
	earliest, latest := "", ""
	for code, days := range touched {
		if len(days) == 0 {
			continue
		}
		ordered := append([]string{}, days...)
		sort.Strings(ordered)
		end, e := time.Parse("2006-01-02", ordered[len(ordered)-1])
		if e != nil {
			return nil, e
		}
		start, e := time.Parse("2006-01-02", ordered[0])
		if e != nil {
			return nil, e
		}
		low, high := start.Format("2006-01-02"), end.AddDate(0, 0, 15).Format("2006-01-02")
		code = strings.ToUpper(code)
		codes = append(codes, code)
		bounds = append(bounds, map[string]string{"code": code, "start": low, "end": high})
		lookupStart := start.AddDate(0, 0, -15).Format("2006-01-02")
		if earliest == "" || lookupStart < earliest {
			earliest = lookupStart
		}
		if high > latest {
			latest = high
		}
	}
	if len(bounds) == 0 {
		return result, nil
	}
	encoded, e := json.Marshal(bounds)
	if e != nil {
		return nil, e
	}
	e = func() error {
		series, e := rates.LoadHistoricalRates(ctx, conn, codes, earliest, latest)
		if e != nil {
			return e
		}
		imports := map[int64]bool{}
		changed := 0
		for _, table := range []string{"bill_game_txs", "bill_topup_txs"} {
			rows, e := conn.QueryContext(ctx, "SELECT id,import_id,date,currency,amount,sign,fx_rate,cny_fen FROM "+table+` t WHERE EXISTS(SELECT 1 FROM json_each(?) b WHERE t.currency=json_extract(b.value,'$.code') AND t.date>=json_extract(b.value,'$.start') AND t.date<=json_extract(b.value,'$.end'))`, string(encoded))
			if e != nil {
				return e
			}
			updates := []map[string]any{}
			for rows.Next() {
				var id, importID int64
				var day, currency string
				var amount float64
				var sign int
				var oldRate sql.NullFloat64
				var oldFen sql.NullInt64
				if e = rows.Scan(&id, &importID, &day, &currency, &amount, &sign, &oldRate, &oldFen); e != nil {
					rows.Close()
					return e
				}
				imports[importID] = true
				value := valueAt(series[currency], currency, day, amount, sign)
				rate, fen := value.Rate, value.Fen
				if (rate == nil) == (!oldRate.Valid) && (rate == nil || *rate == oldRate.Float64) && (fen == nil) == (!oldFen.Valid) && (fen == nil || *fen == oldFen.Int64) {
					continue
				}
				updates = append(updates, map[string]any{"id": id, "rate": rate, "fen": fen})
				changed++
			}
			e = rows.Err()
			rows.Close()
			if e != nil {
				return e
			}
			if len(updates) > 0 {
				raw, e := json.Marshal(updates)
				if e != nil {
					return e
				}
				if _, e = conn.ExecContext(ctx, "UPDATE "+table+` AS t SET fx_rate=json_extract(j.value,'$.rate'),cny_fen=json_extract(j.value,'$.fen') FROM json_each(?) j WHERE t.id=json_extract(j.value,'$.id')`, string(raw)); e != nil {
					return e
				}
			}
		}
		result["bills"], result["transactions"] = len(imports), changed
		return nil
	}()
	if e != nil {
		return nil, e
	}
	return result, nil
}
