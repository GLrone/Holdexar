package derived

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
)

// RefreshBundleSortCache 捆绑包排序缓存刷新回调（nil = 空操作），须在调用方事务连接与写闸内执行。
var RefreshBundleSortCache func(ctx context.Context, conn *sql.Conn, bundleIDs []int64) error

func UpdateMemberBundles(ctx context.Context, conn *sql.Conn, appids []int64) error {
	if len(appids) == 0 {
		return nil
	}
	raw, err := json.Marshal(appids)
	if err != nil {
		return fmt.Errorf("捆绑包联动入参序列化失败: %w", err)
	}
	rows, err := conn.QueryContext(ctx,
		"SELECT DISTINCT p.bundle_id FROM bundle_region_prices p, json_each(p.app_ids) a "+
			"WHERE CAST(a.value AS INTEGER) IN (SELECT value FROM json_each(?))", string(raw))
	if err != nil {
		return fmt.Errorf("捆绑包关联查询失败: %w", err)
	}
	defer rows.Close()
	var ids []int64
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			return fmt.Errorf("捆绑包关联行读取失败: %w", err)
		}
		ids = append(ids, id)
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("捆绑包关联遍历失败: %w", err)
	}
	if len(ids) == 0 || RefreshBundleSortCache == nil {
		return nil
	}
	return RefreshBundleSortCache(ctx, conn, ids)
}

func ReviewReleaseChanged(cur, old CatalogRow) bool {
	return !intPtrEqual(cur.PositiveRate, old.PositiveRate) ||
		!intPtrEqual(cur.ReviewCount, old.ReviewCount) ||
		!strEqual(cur.ReleaseDate, old.ReleaseDate)
}

func intPtrEqual(a, b *int64) bool {
	if a == nil || b == nil {
		return a == b
	}
	return *a == *b
}
