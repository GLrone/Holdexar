// Package wishlist：列表置顶前缀所需的账户关注/愿望单只读查询。
// 与 Python games.service 的 _followed_appids/_wishlist_appids 及
// list_games 的 fav_order+wish_order 排序前缀逐项对齐（只读，不写库）。
package wishlist

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
)

// FollowedAppIDs：关注集（游戏卡星标）。来源 = monitor_sources 的
// target_type='game' + source='favorite' + active=1，去重后升序。
// 对齐 monitoring.ids_with_source("game", "favorite")。
func FollowedAppIDs(ctx context.Context, db *sql.DB) ([]int64, error) {
	rows, err := db.QueryContext(ctx,
		"SELECT DISTINCT target_id FROM monitor_sources"+
			" WHERE target_type = 'game' AND source = 'favorite' AND active = 1"+
			" ORDER BY target_id")
	if err != nil {
		return nil, fmt.Errorf("关注 appid 查询失败: %w", err)
	}
	defer rows.Close()
	out := []int64{}
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			return nil, fmt.Errorf("关注 appid 读取失败: %w", err)
		}
		out = append(out, id)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("关注 appid 遍历失败: %w", err)
	}
	return out, nil
}

// WishlistAppIDs：愿望单成员集 = wishlist_items 中 wishlisted=1 且 active=1
// 的去重 appid，升序。Python 侧取回后 sorted(set(rows))，与 SQL 端
// DISTINCT+ORDER BY 等价（appid 为 NOT NULL 整数主键之一，无 NULL 排序差异）。
func WishlistAppIDs(ctx context.Context, db *sql.DB) ([]int64, error) {
	rows, err := db.QueryContext(ctx,
		"SELECT DISTINCT appid FROM wishlist_items WHERE wishlisted = 1 AND active = 1 ORDER BY appid")
	if err != nil {
		return nil, fmt.Errorf("愿望单 appid 查询失败: %w", err)
	}
	defer rows.Close()
	out := []int64{}
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			return nil, fmt.Errorf("愿望单 appid 读取失败: %w", err)
		}
		out = append(out, id)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("愿望单 appid 遍历失败: %w", err)
	}
	return out, nil
}

// OrderPrefix：置顶前缀（对应 Python wishlist_priority=true 路径）——
// 关注布尔降序在前、愿望单布尔降序在后；空集段不注入，全空返回空串。
// 返回的 SQL 引用列表查询的 games 别名 g。
func OrderPrefix(ctx context.Context, db *sql.DB) (string, []any, error) {
	return orderPrefix(ctx, db, true)
}

// OrderPrefixFor：wishlistPriority=false 时省略愿望单段（Python 默认分支）。
func OrderPrefixFor(ctx context.Context, db *sql.DB, wishlistPriority bool) (string, []any, error) {
	return orderPrefix(ctx, db, wishlistPriority)
}

func orderPrefix(ctx context.Context, db *sql.DB, wishlistPriority bool) (string, []any, error) {
	followed, err := FollowedAppIDs(ctx, db)
	if err != nil {
		return "", nil, err
	}
	var wished []int64
	if wishlistPriority {
		if wished, err = WishlistAppIDs(ctx, db); err != nil {
			return "", nil, err
		}
	}
	var parts []string
	var args []any
	for _, set := range [][]int64{followed, wished} {
		if len(set) == 0 {
			continue
		}
		ph := make([]string, len(set))
		for i := range ph {
			ph[i] = "?"
			args = append(args, set[i])
		}
		parts = append(parts, "g.appid IN ("+strings.Join(ph, ", ")+") DESC")
	}
	return strings.Join(parts, ", "), args, nil
}
