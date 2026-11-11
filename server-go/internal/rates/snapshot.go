package rates

import (
	"context"
	"database/sql"

	_ "holdexar/server-go/internal/derived"
)

func RecomputeCNYPrices(ctx context.Context, conn *sql.Conn) error {
	_, err := recomputeCNY(ctx, conn)
	return err
}

func recomputeCNY(ctx context.Context, conn *sql.Conn) (map[string]int64, error) {
	counts := map[string]int64{}
	for _, table := range []string{"game_current_prices", "bundle_region_prices"} {
		result, err := conn.ExecContext(ctx, "UPDATE "+table+" AS p SET cny_fen=price_cny_fen(p.price,r.rate_to_cny) FROM fx_rates AS r WHERE r.currency_code=UPPER(p.currency) AND p.price IS NOT NULL AND p.price>0")
		if err != nil {
			return nil, err
		}
		count, err := result.RowsAffected()
		if err != nil {
			return nil, err
		}
		key := "games"
		if table == "bundle_region_prices" {
			key = "bundles"
		}
		counts[key] = count
	}
	return counts, nil
}
