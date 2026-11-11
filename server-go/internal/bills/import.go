package bills

import (
	"context"
	"database/sql"
	"encoding/json"
	"math"
	"strings"
	"time"

	"holdexar/server-go/internal/rates"
)

type billValue struct {
	Rate *float64
	Fen  *int64
}

func valueAt(series []rates.HistoricalRate, currency, day string, amount float64, sign int) billValue {
	rate := rates.PickHistoricalRate(series, currency, day)
	if rate == nil {
		return billValue{}
	}
	fen := int64(math.RoundToEven(amount * float64(sign) * *rate * 100))
	return billValue{Rate: rate, Fen: &fen}
}

type billRates struct {
	start, end string
	codes      []string
}

func (r *billRates) include(currency, day string) {
	if currency == "CNY" {
		return
	}
	at, err := time.Parse(time.DateOnly, day)
	if err != nil {
		return
	}
	start := at.AddDate(0, 0, -15).Format(time.DateOnly)
	if r.start == "" || start < r.start {
		r.start = start
	}
	if day > r.end {
		r.end = day
	}
	for _, code := range r.codes {
		if code == currency {
			return
		}
	}
	r.codes = append(r.codes, currency)
}

type licenseKey struct {
	appid           int64
	name, date, acq string
}

func (row licenseTx) key() licenseKey {
	key := licenseKey{name: row.Name, date: row.Date, acq: row.Acq}
	if row.AppID != nil && *row.AppID != 0 {
		key.appid, key.name = *row.AppID, ""
	}
	return key
}

func carryLicensePrices(ctx context.Context, conn *sql.Conn, sid string, incoming []licenseTx) ([]licenseTx, error) {
	rows, err := conn.QueryContext(ctx, `SELECT g.name,g.appid,g.date,g.acq,g.acq_label,g.manual_fen FROM bill_cdk_games g JOIN bill_imports b ON b.id=g.import_id WHERE b.steam_id=? AND g.manual_fen IS NOT NULL ORDER BY g.id`, sid)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	previous := []licenseTx{}
	byKey := map[licenseKey][]int{}
	for rows.Next() {
		var row licenseTx
		if err = rows.Scan(&row.Name, &row.AppID, &row.Date, &row.Acq, &row.AcqLabel, &row.ManualFen); err != nil {
			return nil, err
		}
		key := row.key()
		byKey[key] = append(byKey[key], len(previous))
		previous = append(previous, row)
	}
	if err = rows.Err(); err != nil {
		return nil, err
	}
	result := make([]licenseTx, 0, len(incoming)+len(previous))
	for _, row := range incoming {
		key := row.key()
		if indices := byKey[key]; len(indices) > 0 {
			old := &previous[indices[0]]
			row.ManualFen, old.ManualFen = old.ManualFen, nil
			byKey[key] = indices[1:]
		}
		result = append(result, row)
	}
	for _, row := range previous {
		if row.ManualFen != nil {
			result = append(result, row)
		}
	}
	return result, nil
}

func insertBillRows(ctx context.Context, conn *sql.Conn, query string, importID int64, values any) error {
	raw, err := json.Marshal(values)
	if err != nil {
		return err
	}
	_, err = conn.ExecContext(ctx, query, importID, string(raw))
	return err
}

type valuedGame struct {
	gameTx
	billValue
	Note string
}

type valuedTopup struct {
	topupTx
	billValue
	Note string
}

func gameNote(tx gameTx) string {
	notes := []string{}
	if tx.Type == "游戏内购买" {
		notes = append(notes, "内购")
	}
	if tx.Refund {
		notes = append(notes, "退款")
	}
	if tx.Gift {
		notes = append(notes, "礼物")
	}
	return strings.Join(notes, " | ")
}

func insertTransactions(ctx context.Context, conn *sql.Conn, id int64, report parsedReport) (int, error) {
	var window billRates
	for _, row := range report.Game {
		window.include(row.Currency, row.Date)
	}
	for _, row := range report.Topup {
		window.include(row.Currency, row.Date)
	}
	series := map[string][]rates.HistoricalRate{}
	if len(window.codes) > 0 {
		var err error
		series, err = rates.LoadHistoricalRates(ctx, conn, window.codes, window.start, window.end)
		if err != nil {
			return 0, err
		}
	}
	missing := 0
	if len(report.Game) > 0 {
		games := make([]valuedGame, 0, len(report.Game))
		for _, tx := range report.Game {
			v := valueAt(series[tx.Currency], tx.Currency, tx.Date, tx.Amount, tx.Sign)
			if v.Rate == nil {
				missing++
			}
			games = append(games, valuedGame{tx, v, gameNote(tx)})
		}
		if err := insertBillRows(ctx, conn, `INSERT INTO bill_game_txs(import_id,date,tx_type,items_json,currency,amount,sign,cny_fen,fx_rate,fx_note,payment,discount_pct,orig_price,wallet_balance,base_price,payment_parts_json,gift_to_json,is_gift,is_refund,orig_is_gift,raw_idx)
SELECT ?,value->>'Date',value->>'Type',COALESCE(value->>'Items','[]'),value->>'Currency',value->>'Amount',value->>'Sign',value->>'Fen',value->>'Rate',value->>'Note',value->>'Payment',value->>'Discount',value->>'OrigPrice',value->>'WalletBalance',value->>'BasePrice',value->>'PaymentParts',NULLIF(value->>'GiftTo','[]'),value->>'Gift',value->>'Refund',value->>'OrigGift',value->>'RawIdx' FROM json_each(?)`, id, games); err != nil {
			return 0, err
		}
	}
	if len(report.Topup) > 0 {
		topups := make([]valuedTopup, 0, len(report.Topup))
		for _, tx := range report.Topup {
			v := valueAt(series[tx.Currency], tx.Currency, tx.Date, tx.Amount, tx.Sign)
			if v.Rate == nil {
				missing++
			}
			note := ""
			if tx.Refund {
				note = "退款"
			}
			topups = append(topups, valuedTopup{tx, v, note})
		}
		if err := insertBillRows(ctx, conn, `INSERT INTO bill_topup_txs(import_id,date,"desc",currency,amount,sign,cny_fen,fx_rate,fx_note,payment,tx_type,is_refund)
SELECT ?,value->>'Date',value->>'Desc',value->>'Currency',value->>'Amount',value->>'Sign',value->>'Fen',value->>'Rate',value->>'Note',value->>'Payment',value->>'Type',value->>'Refund' FROM json_each(?)`, id, topups); err != nil {
			return 0, err
		}
	}
	return missing, nil
}

func importBill(ctx context.Context, conn *sql.Conn, sid string, report parsedReport, at time.Time) (map[string]any, error) {
	licenses, err := carryLicensePrices(ctx, conn, sid, report.Licenses)
	if err != nil {
		return nil, err
	}
	warnings := append([]string{}, report.Warnings...)
	raw, err := json.Marshal(warnings)
	if err != nil {
		return nil, err
	}
	created, err := conn.ExecContext(ctx, `INSERT INTO bill_imports(steam_id,nickname,avatar_base64,source_file,imported_at,warnings_json) VALUES(?,?,?,?,?,?)`, sid, report.Nickname, report.Avatar, "auto-sync "+at.Format(time.DateOnly), at.Format("2006-01-02 15:04:05.000000"), string(raw))
	if err != nil {
		return nil, err
	}
	id, err := created.LastInsertId()
	if err != nil {
		return nil, err
	}
	missing, err := insertTransactions(ctx, conn, id, report)
	if err != nil {
		return nil, err
	}
	if len(licenses) > 0 {
		if err = insertBillRows(ctx, conn, `INSERT INTO bill_cdk_games(import_id,name,appid,date,acq,acq_label,manual_fen)
SELECT ?,value->>'Name',value->>'AppID',value->>'Date',value->>'Acq',value->>'AcqLabel',value->>'ManualFen' FROM json_each(?)`, id, licenses); err != nil {
			return nil, err
		}
	}
	// Keep old IDs occupied until all replacement rows have been inserted.
	for _, table := range []string{"bill_game_txs", "bill_topup_txs", "bill_cdk_games"} {
		if _, err = conn.ExecContext(ctx, `DELETE FROM `+table+` WHERE import_id IN (SELECT id FROM bill_imports WHERE steam_id=? AND id<>?)`, sid, id); err != nil {
			return nil, err
		}
	}
	deleted, err := conn.ExecContext(ctx, `DELETE FROM bill_imports WHERE steam_id=? AND id<>?`, sid, id)
	if err != nil {
		return nil, err
	}
	replaced, err := deleted.RowsAffected()
	if err != nil {
		return nil, err
	}
	return map[string]any{"importId": id, "steamId": sid, "nickname": report.Nickname, "gameTxs": len(report.Game), "topupTxs": len(report.Topup), "cdkGames": len(report.Licenses), "fxMissing": missing, "warnings": warnings, "replaced": replaced}, nil
}
