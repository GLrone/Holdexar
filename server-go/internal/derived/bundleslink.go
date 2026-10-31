// 捆绑包联动挂点：评测/发行日变化触发 bundles 排序缓存刷新（对齐现网
// update_member_bundles → refresh_bundle_sort_cache 链）。
//
// 现网触发条件（两处）：
//  1. update_price_derived 携目录快照时——(positive_rate, review_count,
//     release_date) 任一变化（目录快照 values[3:6] 元组比较；快照无键的新行
//     视为变化，已删行不触发），对受影响 appid 集触发；
//  2. maintain_derived 日切换（derived_inputs.day 变化）——对全部
//     COALESCE(review_count,0)=0 的行触发一次（评分未定型的行随日刷新）。
//
// 影响面：受影响 appid 出现在 bundle_region_prices.app_ids（JSON 数组）中的
// 捆绑包，其排序缓存（bundle 域 service 层）在调用方事务连接内刷新。
//
// bundles 域未迁移：RefreshBundleSortCache 保持默认 nil（空操作），本文件只
// 落 appid → bundle_id 解析与回调挂点；退出条件 = bundles 域迁移批次以真实
// 排序缓存实现注入回调，并在调用侧接通（目录联动分支与日切换两处）。
package derived

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
)

// RefreshBundleSortCache 捆绑包排序缓存刷新回调；bundles 域迁移批次注入，
// 默认 nil = 空操作。在调用方事务连接与写闸内执行。
var RefreshBundleSortCache func(ctx context.Context, conn *sql.Conn, bundleIDs []int64) error

// UpdateMemberBundles 解析受影响 appid 所在捆绑包并触发排序缓存刷新
// （无关联捆绑包或回调未注入时为空操作）。
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

// ReviewReleaseChanged 评测/发行日联动判定：目录行 (positive_rate,
// review_count, release_date) 任一变化。调用方对快照无键的行直接视为变化
// （Python 空元组比较同径）。
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
