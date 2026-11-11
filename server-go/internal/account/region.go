package account

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
)

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
