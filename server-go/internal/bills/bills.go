package bills

import (
	"context"
	"database/sql"
	"strings"
)

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

func PyIsoformat(v string) string {
	if v == "" {
		return v
	}
	if i := strings.IndexByte(v, ' '); i >= 0 {
		v = v[:i] + "T" + v[i+1:]
	}
	return strings.TrimSuffix(v, ".000000")
}

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
