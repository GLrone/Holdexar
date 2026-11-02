// bills 只读域：本地账单导入记录的聚合读（消费合计），无凭据无出网无写。
// 对齐 Python bills_service.list_imports（domains/bills/service.py）。
package bills

import (
	"context"
	"database/sql"
	"strings"
)

// ImportRecord 单次账单导入的聚合行（金额口径：人民币分）。
type ImportRecord struct {
	ID            int64
	SteamID       sql.NullString
	Nickname      sql.NullString
	SourceFile    sql.NullString
	ImportedAt    sql.NullString
	GameNetFen    int64
	GameSpendFen  int64
	GameRefundFen int64
	Orders        int64
	FxMissing     int64
}

// PyIsoformat 对齐 datetime.isoformat：空格→T 分隔、微秒为 0 时省略小数段、
// 解析失败原样返回（与 games 包 goIsoformat 同径，bills 包自持）。
func PyIsoformat(v string) string {
	if v == "" {
		return v
	}
	if i := strings.IndexByte(v, ' '); i >= 0 {
		v = v[:i] + "T" + v[i+1:]
	}
	return strings.TrimSuffix(v, ".000000")
}

// ListImports 已导入账单列表（新→旧）：BillImport LEFT JOIN 按区交易账本，
// spend=非退款 cny_fen 合计、refund=退款 |cny_fen| 合计、orders=非退款笔数、
// fxMissing=cny_fen 缺失笔数（含退款行）。
func ListImports(ctx context.Context, db *sql.DB) ([]ImportRecord, error) {
	rows, err := db.QueryContext(ctx, `
SELECT b.id, b.steam_id, b.nickname, b.source_file, CAST(b.imported_at AS TEXT),
       COALESCE(SUM(CASE WHEN t.is_refund = 0 THEN t.cny_fen ELSE 0 END), 0),
       COALESCE(SUM(CASE WHEN t.is_refund = 1 THEN ABS(t.cny_fen) ELSE 0 END), 0),
       COUNT(CASE WHEN t.is_refund = 0 THEN t.id END),
       COUNT(CASE WHEN t.cny_fen IS NULL THEN t.id END)
FROM bill_imports b
LEFT JOIN bill_game_txs t ON t.import_id = b.id
GROUP BY b.id
ORDER BY b.id DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	out := make([]ImportRecord, 0, 8)
	for rows.Next() {
		var r ImportRecord
		if err := rows.Scan(&r.ID, &r.SteamID, &r.Nickname, &r.SourceFile,
			&r.ImportedAt, &r.GameSpendFen, &r.GameRefundFen, &r.Orders, &r.FxMissing); err != nil {
			return nil, err
		}
		r.GameNetFen = r.GameSpendFen - r.GameRefundFen
		r.ImportedAt.String = PyIsoformat(r.ImportedAt.String)
		out = append(out, r)
	}
	return out, rows.Err()
}
