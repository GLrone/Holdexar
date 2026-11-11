package migration

import (
	"context"
	"database/sql"
	"time"

	"holdexar/server-go/internal/bundles"
	"holdexar/server-go/internal/derived"
	"holdexar/server-go/internal/rates"
)

func migratePriceSnapshot(ctx context.Context, conn *sql.Conn) error {
	dataDir, err := migrationDataDir(ctx, conn)
	if err != nil {
		return err
	}
	overrides, order := derived.LoadSeriesOverrides(dataDir)
	now := time.Now().In(time.FixedZone("Asia/Shanghai", 8*60*60))
	if err := rates.RecomputeCNYPrices(ctx, conn); err != nil {
		return err
	}
	if _, err := derived.UpdatePriceDerived(ctx, conn, nil, derived.Options{Now: now, History: true}); err != nil {
		return err
	}
	if _, err := derived.RefreshSeries(ctx, conn, nil, overrides, order); err != nil {
		return err
	}
	rows, err := conn.QueryContext(ctx, "SELECT UPPER(code) FROM crawl_regions WHERE enabled=1")
	if err != nil {
		return err
	}
	var tracked []string
	for rows.Next() {
		var code string
		if err := rows.Scan(&code); err != nil {
			rows.Close()
			return err
		}
		tracked = append(tracked, code)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	if _, err := bundles.RefreshSortCache(ctx, conn, nil, tracked); err != nil {
		return err
	}
	return derived.SaveInputVersion(ctx, conn, now, overrides)
}
