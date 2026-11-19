package games

import (
	"context"
	"database/sql"
	"strings"
)

const ftsAppidCap = 1000
const ftsMaxTokens = 32

// SearchCatalog 检索目录名（不做价格 JOIN）：优先 games_name_fts，无命中或不可用时退回 LIKE。
func SearchCatalog(ctx context.Context, db *sql.DB, term string, limit int) ([]map[string]any, error) {
	q := strings.TrimSpace(term)
	if q == "" || limit <= 0 {
		return []map[string]any{}, nil
	}
	appids, err := ftsAppids(ctx, db, q)
	if err != nil {
		appids = nil
	}
	if len(appids) > 0 {
		rows, err := catalogRows(ctx, db, appids, "", limit)
		if err != nil {
			return nil, err
		}
		if len(rows) > 0 {
			return rows, nil
		}
	}
	return catalogRows(ctx, db, nil, q, limit)
}

// ftsAppids 经 name_bigram 分词匹配 FTS；无可用 token 时返回空集。
func ftsAppids(ctx context.Context, db *sql.DB, q string) ([]int64, error) {
	var bi string
	if err := db.QueryRowContext(ctx, "SELECT name_bigram(?, '')", q).Scan(&bi); err != nil {
		return nil, err
	}
	pieces := strings.Fields(bi)
	if len(pieces) == 0 {
		return nil, nil
	}
	if len(pieces) > ftsMaxTokens {
		pieces = pieces[:ftsMaxTokens]
	}
	quoted := make([]string, len(pieces))
	for i, p := range pieces {
		quoted[i] = `"` + strings.ReplaceAll(p, `"`, `""`) + `"`
	}
	rows, err := db.QueryContext(ctx,
		"SELECT appid FROM games_name_fts WHERE games_name_fts MATCH ?",
		strings.Join(quoted, " OR "))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var appids []int64
	for rows.Next() {
		var appid int64
		if err := rows.Scan(&appid); err != nil {
			return nil, err
		}
		if len(appids) < ftsAppidCap {
			appids = append(appids, appid)
		}
	}
	return appids, rows.Err()
}

func catalogRows(ctx context.Context, db *sql.DB, appids []int64, q string, limit int) ([]map[string]any, error) {
	base := "SELECT g.appid, g.name, g.name_en, g.positive_rate, g.review_count," +
		" cp.cny_fen, cp.discount_percent FROM games g" +
		" LEFT JOIN game_current_prices cp ON cp.appid = g.appid AND cp.region_code = 'CN'" +
		" AND cp.price_status = 'ok' WHERE "
	var args []any
	if len(appids) > 0 {
		ph := strings.TrimSuffix(strings.Repeat("?,", len(appids)), ",")
		base += "g.appid IN (" + ph + ")"
		for _, a := range appids {
			args = append(args, a)
		}
	} else {
		like := "%" + q + "%"
		base += "(g.name LIKE ? OR g.name_en LIKE ?)"
		args = append(args, like, like)
	}
	base += " AND g.appid NOT IN (SELECT appid FROM catalog_removals)" +
		" ORDER BY g.review_count DESC LIMIT ?"
	args = append(args, limit)

	rows, err := db.QueryContext(ctx, base, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make([]map[string]any, 0)
	for rows.Next() {
		var appid int64
		var name string
		var nameEn *string
		var positiveRate *int64
		var reviewCount int64
		var cnyFen *int64
		var discount *int64
		if err := rows.Scan(&appid, &name, &nameEn, &positiveRate, &reviewCount, &cnyFen, &discount); err != nil {
			return nil, err
		}
		item := map[string]any{
			"appid":        appid,
			"name":         name,
			"nameEn":       nameEn,
			"basePriceFen": cnyFen,
			"discount":     discountValue(discount),
			"positiveRate": rateRatio(positiveRate),
			"reviewCount":  reviewCount,
		}
		out = append(out, item)
	}
	return out, rows.Err()
}

func discountValue(d *int64) int64 {
	if d == nil {
		return 0
	}
	return *d
}
