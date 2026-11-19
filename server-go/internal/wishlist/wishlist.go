package wishlist

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
)

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

// OrderPrefix 返回列表置顶排序 SQL 片段（引用 games 别名 g）；空集返回空串。
func OrderPrefix(ctx context.Context, db *sql.DB) (string, []any, error) {
	return orderPrefix(ctx, db, true)
}

// OrderPrefixFor：wishlistPriority=false 时省略愿望单段。
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
