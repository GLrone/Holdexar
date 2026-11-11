package bills

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"slices"
	"strings"

	"holdexar/server-go/internal/store"
)

type billAmounts struct {
	NetFen    int64 `json:"netFen"`
	SpendFen  int64 `json:"spendFen"`
	RefundFen int64 `json:"refundFen"`
}

func billImportViews(ctx context.Context, db *sql.DB) (any, error) {
	records, err := ListImports(ctx, db)
	if err != nil {
		return nil, err
	}
	imports := make([]map[string]any, 0, len(records))
	for _, r := range records {
		imports = append(imports, map[string]any{
			"id": r.ID, "steamId": billNullString(r.SteamID), "nickname": billNullString(r.Nickname),
			"sourceFile": billNullString(r.SourceFile), "importedAt": billNullString(r.ImportedAt),
			"gameNetFen": r.GameNetFen, "gameSpendFen": r.GameSpendFen, "gameRefundFen": r.GameRefundFen,
			"orders": r.Orders, "fxMissing": r.FxMissing,
		})
	}
	return map[string]any{"imports": imports}, nil
}

func billNullString(v sql.NullString) any {
	if v.Valid {
		return v.String
	}
	return nil
}

func billJSON(raw *string, optionalArray bool) (json.RawMessage, error) {
	if raw == nil || *raw == "" {
		if optionalArray {
			return nil, nil
		}
		return json.RawMessage("[]"), nil
	}
	data := json.RawMessage(*raw)
	if optionalArray {
		if !strings.HasPrefix(strings.TrimSpace(*raw), "[") || !json.Valid(data) {
			return nil, nil
		}
	} else if !json.Valid(data) {
		return nil, errors.New("账单中存在无效的 JSON 数据")
	}
	return data, nil
}

type billMonth struct {
	billAmounts
	Month string `json:"month"`
	Count int64  `json:"count"`
}

type billMonthInYear struct {
	NetFen int64 `json:"netFen"`
	Count  int64 `json:"count"`
}

type billYear struct {
	billAmounts
	Year   string                     `json:"year"`
	Count  int64                      `json:"count"`
	Months map[string]billMonthInYear `json:"months"`
}

type billSummary struct {
	billAmounts
	Orders          int64 `json:"orders"`
	TxCount         int64 `json:"txCount"`
	FxMissing       int64 `json:"fxMissing"`
	BuyTotalFen     int64 `json:"buyTotalFen"`
	BuyRefundFen    int64 `json:"buyRefundFen"`
	GiftTotalFen    int64 `json:"giftTotalFen"`
	GiftRefundFen   int64 `json:"giftRefundFen"`
	SelfNetFen      int64 `json:"selfNetFen"`
	GiftNetFen      int64 `json:"giftNetFen"`
	QuotaFen        int64 `json:"quotaFen"`
	AccountValueFen int64 `json:"accountValueFen"`
	CdkPriced       int64 `json:"cdkPriced"`
	CdkTotalFen     int64 `json:"cdkTotalFen"`
	TopupNetFen     int64 `json:"topupNetFen"`
	TopupSpendFen   int64 `json:"topupSpendFen"`
	TopupRefundFen  int64 `json:"topupRefundFen"`
}

type billTopupCurrency struct {
	Currency string  `json:"currency"`
	Total    float64 `json:"total"`
	CnyFen   int64   `json:"cnyFen"`
	Count    int64   `json:"count"`
}

func billOverview(ctx context.Context, db *sql.DB, id int64) (any, error) {
	tx, err := db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	var steamID, nickname, avatar, sourceFile, importedAt, rawWarnings *string
	err = tx.QueryRowContext(ctx, `
SELECT steam_id, nickname, avatar_base64, source_file, CAST(imported_at AS TEXT), warnings_json
FROM bill_imports WHERE id = ?`, id).Scan(&steamID, &nickname, &avatar, &sourceFile, &importedAt, &rawWarnings)
	if err != nil {
		return nil, err
	}
	if importedAt != nil {
		*importedAt = PyIsoformat(*importedAt)
	}
	if avatar != nil && *avatar != "" && !strings.HasPrefix(*avatar, "data:") {
		*avatar = "data:image/png;base64," + *avatar
	}
	warnings, err := billJSON(rawWarnings, false)
	if err != nil {
		return nil, err
	}

	var summary billSummary
	years, months, err := billGameStats(ctx, tx, id, &summary)
	if err != nil {
		return nil, err
	}
	topups, topupCount, err := billTopupStats(ctx, tx, id, &summary)
	if err != nil {
		return nil, err
	}
	var cdkCount, cdk, gift, free int64
	err = tx.QueryRowContext(ctx, `
SELECT COUNT(*), COUNT(CASE WHEN acq = 'cdk' THEN 1 END),
       COUNT(CASE WHEN acq = 'gift' THEN 1 END), COUNT(CASE WHEN acq = 'free' THEN 1 END),
       COUNT(CASE WHEN acq IS NOT 'free' THEN manual_fen END),
       COALESCE(SUM(CASE WHEN acq IS NOT 'free' THEN manual_fen END), 0)
FROM bill_cdk_games WHERE import_id = ?`, id).Scan(&cdkCount, &cdk, &gift, &free, &summary.CdkPriced, &summary.CdkTotalFen)
	if err != nil {
		return nil, err
	}
	summary.AccountValueFen = summary.SelfNetFen + summary.CdkTotalFen
	return map[string]any{
		"id": id, "steamId": steamID, "nickname": nickname, "avatar": avatar, "sourceFile": sourceFile,
		"importedAt": importedAt, "warnings": warnings, "summary": summary,
		"years": years, "monthSeries": months, "topupByCurrency": topups,
		"counts": map[string]int64{"gameTxs": summary.TxCount, "topupTxs": topupCount, "cdkGames": cdkCount, "cdk": cdk, "gift": gift, "free": free},
	}, nil
}

func billGameStats(ctx context.Context, tx *sql.Tx, id int64, summary *billSummary) ([]billYear, []billMonth, error) {
	rows, err := tx.QueryContext(ctx, `
SELECT substr(date, 1, 4), substr(date, 1, 7), substr(date, 6, 2),
       COALESCE(SUM(cny_fen), 0),
       COALESCE(SUM(CASE WHEN NOT is_refund THEN cny_fen END), 0),
       COALESCE(SUM(CASE WHEN is_refund THEN ABS(cny_fen) END), 0),
       COUNT(*), COUNT(CASE WHEN NOT is_refund THEN 1 END), COUNT(*) - COUNT(cny_fen),
       COALESCE(SUM(CASE WHEN NOT is_refund AND is_gift THEN cny_fen END), 0),
       COALESCE(SUM(CASE WHEN is_refund AND orig_is_gift THEN ABS(cny_fen) END), 0)
FROM bill_game_txs WHERE import_id = ? GROUP BY 1, 2, 3 ORDER BY 2 DESC`, id)
	if err != nil {
		return nil, nil, err
	}
	defer rows.Close()
	years, months := []billYear{}, []billMonth{}
	for rows.Next() {
		var m billMonth
		var year, monthInYear string
		var orders, missing, giftTotal, giftRefund int64
		err := rows.Scan(&year, &m.Month, &monthInYear, &m.NetFen, &m.SpendFen, &m.RefundFen,
			&m.Count, &orders, &missing, &giftTotal, &giftRefund)
		if err != nil {
			return nil, nil, err
		}
		months = append(months, m)
		if len(years) == 0 || years[len(years)-1].Year != year {
			years = append(years, billYear{Year: year, Months: map[string]billMonthInYear{}})
		}
		y := &years[len(years)-1]
		y.NetFen += m.NetFen
		y.SpendFen += m.SpendFen
		y.RefundFen += m.RefundFen
		y.Count += m.Count
		ym := y.Months[monthInYear]
		ym.NetFen += m.NetFen
		ym.Count += m.Count
		y.Months[monthInYear] = ym
		summary.SpendFen += m.SpendFen
		summary.RefundFen += m.RefundFen
		summary.TxCount += m.Count
		summary.Orders += orders
		summary.FxMissing += missing
		summary.GiftTotalFen += giftTotal
		summary.GiftRefundFen += giftRefund
	}
	slices.Reverse(months)
	summary.NetFen = summary.SpendFen - summary.RefundFen
	summary.BuyTotalFen = summary.SpendFen - summary.GiftTotalFen
	summary.BuyRefundFen = summary.RefundFen - summary.GiftRefundFen
	summary.SelfNetFen = summary.BuyTotalFen - summary.BuyRefundFen
	summary.GiftNetFen = summary.GiftTotalFen - summary.GiftRefundFen
	summary.QuotaFen = summary.SelfNetFen - summary.GiftNetFen
	return years, months, rows.Err()
}

func billTopupStats(ctx context.Context, tx *sql.Tx, id int64, summary *billSummary) ([]billTopupCurrency, int64, error) {
	rows, err := tx.QueryContext(ctx, `
SELECT currency, SUM(amount * sign), COALESCE(SUM(cny_fen), 0), COUNT(*),
       COALESCE(SUM(CASE WHEN NOT is_refund THEN cny_fen END), 0),
       COALESCE(SUM(CASE WHEN is_refund THEN ABS(cny_fen) END), 0)
FROM bill_topup_txs WHERE import_id = ? GROUP BY currency ORDER BY currency`, id)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()
	topups := []billTopupCurrency{}
	var count int64
	for rows.Next() {
		var c billTopupCurrency
		var spend, refund int64
		if err := rows.Scan(&c.Currency, &c.Total, &c.CnyFen, &c.Count, &spend, &refund); err != nil {
			return nil, 0, err
		}
		topups = append(topups, c)
		count += c.Count
		summary.TopupSpendFen += spend
		summary.TopupRefundFen += refund
	}
	summary.TopupNetFen = summary.TopupSpendFen - summary.TopupRefundFen
	return topups, count, rows.Err()
}

type billTxAmounts struct {
	ID       int64    `json:"id"`
	Date     string   `json:"date"`
	TxType   string   `json:"txType"`
	Currency string   `json:"currency"`
	Amount   float64  `json:"amount"`
	Sign     int      `json:"sign"`
	CnyFen   *int64   `json:"cnyFen"`
	FxRate   *float64 `json:"fxRate"`
	Payment  string   `json:"payment"`
	IsRefund bool     `json:"isRefund"`
}

type billGameTx struct {
	billTxAmounts
	Items         json.RawMessage `json:"items"`
	FxNote        string          `json:"fxNote"`
	DiscountPct   string          `json:"discountPct"`
	OrigPrice     *float64        `json:"origPrice"`
	WalletBalance *string         `json:"walletBalance"`
	BasePrice     *string         `json:"basePrice"`
	PaymentParts  json.RawMessage `json:"paymentParts"`
	GiftTo        json.RawMessage `json:"giftTo"`
	IsGift        bool            `json:"isGift"`
	OrigIsGift    bool            `json:"origIsGift"`
}

type billTopupTx struct {
	billTxAmounts
	Desc string `json:"desc"`
}

type billCdkPrice struct {
	ID        int64  `json:"id"`
	Name      string `json:"name"`
	ManualFen *int64 `json:"manualFen"`
}

type billCdk struct {
	billCdkPrice
	AppID    *int64 `json:"appid"`
	Date     string `json:"date"`
	Acq      string `json:"acq"`
	AcqLabel string `json:"acqLabel"`
}

func scanBillGame(rows *sql.Rows) (billGameTx, error) {
	var v billGameTx
	var items, paymentParts, giftTo *string
	err := rows.Scan(&v.ID, &v.Date, &v.TxType, &items, &v.Currency, &v.Amount, &v.Sign, &v.CnyFen, &v.FxRate,
		&v.FxNote, &v.Payment, &v.DiscountPct, &v.OrigPrice, &v.WalletBalance, &v.BasePrice,
		&paymentParts, &giftTo, &v.IsGift, &v.IsRefund, &v.OrigIsGift)
	if err != nil {
		return v, err
	}
	v.Items, err = billJSON(items, false)
	v.PaymentParts, _ = billJSON(paymentParts, true)
	v.GiftTo, _ = billJSON(giftTo, true)
	return v, err
}

func scanBillTopup(rows *sql.Rows) (billTopupTx, error) {
	var v billTopupTx
	err := rows.Scan(&v.ID, &v.Date, &v.Desc, &v.Currency, &v.Amount, &v.Sign, &v.CnyFen, &v.FxRate,
		&v.Payment, &v.TxType, &v.IsRefund)
	return v, err
}

func scanBillCdk(rows *sql.Rows) (billCdk, error) {
	var v billCdk
	err := rows.Scan(&v.ID, &v.Name, &v.AppID, &v.Date, &v.Acq, &v.AcqLabel, &v.ManualFen)
	return v, err
}

func billPage[T any](ctx context.Context, db *sql.DB, columns, from string, args []any, limit, offset int64, scan func(*sql.Rows) (T, error)) (any, error) {
	tx, err := db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	var total int64
	if err := tx.QueryRowContext(ctx, "SELECT COUNT(*) "+from, args...).Scan(&total); err != nil {
		return nil, err
	}
	rows, err := tx.QueryContext(ctx, "SELECT "+columns+" "+from+" ORDER BY date DESC, id DESC LIMIT ? OFFSET ?", append(args, limit, offset)...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := []T{}
	for rows.Next() {
		item, err := scan(rows)
		if err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	return map[string]any{"total": total, "rows": items, "limit": limit, "offset": offset}, rows.Err()
}

func deleteBillImport(ctx context.Context, gate *store.WriteGate, id int64) (any, error) {
	var removed int64
	err := gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		for _, table := range []string{"bill_game_txs", "bill_topup_txs", "bill_cdk_games"} {
			if _, err := conn.ExecContext(ctx, "DELETE FROM "+table+" WHERE import_id = ?", id); err != nil {
				return err
			}
		}
		result, err := conn.ExecContext(ctx, "DELETE FROM bill_imports WHERE id = ?", id)
		if err != nil {
			return err
		}
		removed, err = result.RowsAffected()
		return err
	})
	return map[string]any{"importId": id, "deleted": removed > 0}, err
}

func updateBillCdkPrice(ctx context.Context, gate *store.WriteGate, id int64, fen *int64) (any, error) {
	var row billCdkPrice
	err := gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		return conn.QueryRowContext(ctx, "UPDATE bill_cdk_games SET manual_fen = ? WHERE id = ? RETURNING id, name, manual_fen", fen, id).
			Scan(&row.ID, &row.Name, &row.ManualFen)
	})
	return row, err
}
