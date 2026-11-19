package account

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"strings"
)

var currencyPrimaryRegion = map[string]string{
	"USD": "us",
}

func deriveRegion(ctx context.Context, db *sql.DB, currencyCode, regionName string) string {
	code := strings.ToUpper(strings.TrimSpace(currencyCode))
	name := strings.TrimSpace(regionName)
	if code == "" || db == nil {
		return ""
	}
	rows, err := db.QueryContext(ctx, `SELECT code, name FROM crawl_regions WHERE currency = ?`, code)
	if err != nil {
		return ""
	}
	defer rows.Close()
	type regionRow struct{ code, name string }
	var matches []regionRow
	for rows.Next() {
		var r regionRow
		if err := rows.Scan(&r.code, &r.name); err != nil {
			return ""
		}
		matches = append(matches, r)
	}
	if rows.Err() != nil || len(matches) == 0 {
		return ""
	}
	if len(matches) > 1 {
		if name != "" {
			for _, m := range matches {
				if m.name == name {
					return m.code
				}
			}
			for _, m := range matches {
				if strings.HasPrefix(m.name, name) {
					return m.code
				}
			}
		}
		if primary, ok := currencyPrimaryRegion[code]; ok {
			return primary
		}
	}
	return matches[0].code
}

type WalletRegionQuery interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
}

func PrimaryWalletRegion(ctx context.Context, q WalletRegionQuery) (string, error) {
	var raw sql.NullString
	e := q.QueryRowContext(ctx, `SELECT wallet_json FROM steam_accounts ORDER BY bound_at,steam_id LIMIT 1`).Scan(&raw)
	if errors.Is(e, sql.ErrNoRows) || e == nil && (!raw.Valid || raw.String == "null") {
		return "", nil
	}
	if e != nil {
		return "", e
	}
	var wallet map[string]any
	if e = json.Unmarshal([]byte(raw.String), &wallet); e != nil {
		return "", nil
	}
	value := wallet["region_code"]
	if value == nil {
		return "", nil
	}
	if s, ok := value.(string); ok {
		return s, nil
	}
	return "", nil
}
