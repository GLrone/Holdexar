package account

import (
	"context"
	"database/sql"
	"fmt"
	"sort"
)

// ItemFlags 监控池条目的工具消费面投影：list_wishlist / list_owned 只取
// 这两个来源标记（名称/封面由 BriefsFor 另查，不随行带出）。
type ItemFlags struct {
	AppID      int64
	Wishlisted bool
	Owned      bool
}

// ListItems 监控池条目并集（Python wishlist.service.list_items 缺省全量路径；
// steamid 入参工具不传，不移植）：
// 1) wishlist_items 活跃行按 appid 归并（标记任一账户命中即真）；
// 2) 监控资格集补入无账户行条目，并挂 family_wishlist / owned 来源标记；
// 3) 被用户移出（monitor_targets 判定 excluded）的对象剔除；
// 4) appid 升序返回。
func ListItems(ctx context.Context, db *sql.DB) ([]ItemFlags, error) {
	rows, err := db.QueryContext(ctx,
		"SELECT appid, wishlisted, owned FROM wishlist_items WHERE active = 1")
	if err != nil {
		return nil, fmt.Errorf("监控条目查询失败: %w", err)
	}
	merged := map[int64]*ItemFlags{}
	err = func() error {
		defer rows.Close()
		for rows.Next() {
			var appid int64
			var wishlisted, owned bool
			if err := rows.Scan(&appid, &wishlisted, &owned); err != nil {
				return fmt.Errorf("监控条目读取失败: %w", err)
			}
			f := merged[appid]
			if f == nil {
				f = &ItemFlags{AppID: appid}
				merged[appid] = f
			}
			f.Wishlisted = f.Wishlisted || wishlisted
			f.Owned = f.Owned || owned
		}
		return rows.Err()
	}()
	if err != nil {
		return nil, fmt.Errorf("监控条目遍历失败: %w", err)
	}

	activeIDs, err := activeGameIDs(ctx, db)
	if err != nil {
		return nil, err
	}
	for _, id := range activeIDs {
		if _, ok := merged[id]; !ok {
			merged[id] = &ItemFlags{AppID: id}
		}
	}
	srcs, err := gameSources(ctx, db, activeIDs)
	if err != nil {
		return nil, err
	}
	for id, set := range srcs {
		if f, ok := merged[id]; ok {
			f.Wishlisted = f.Wishlisted || set["family_wishlist"]
			f.Owned = f.Owned || set["owned"]
		}
	}

	ids := make([]int64, 0, len(merged))
	for id := range merged {
		ids = append(ids, id)
	}
	excluded, err := excludedGameIDs(ctx, db, ids)
	if err != nil {
		return nil, err
	}
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
	out := make([]ItemFlags, 0, len(ids))
	for _, id := range ids {
		if excluded[id] {
			continue
		}
		f := merged[id]
		out = append(out, ItemFlags{AppID: id, Wishlisted: f.Wishlisted, Owned: f.Owned})
	}
	return out, nil
}

// activeGameIDs 监控资格集（Python monitoring.active_ids）：激活来源且未被
// 排除的对象；released 不受排除门影响。排序不作输出契约（最终按 appid 排序）。
func activeGameIDs(ctx context.Context, db *sql.DB) ([]int64, error) {
	rows, err := db.QueryContext(ctx,
		"SELECT DISTINCT ms.target_id FROM monitor_sources ms"+
			" JOIN monitor_targets mt ON mt.target_type = ms.target_type AND mt.target_id = ms.target_id"+
			" WHERE ms.target_type = 'game' AND ms.active = 1"+
			" AND NOT EXISTS (SELECT 1 FROM monitor_exclusions me"+
			" WHERE me.target_type = ms.target_type AND me.target_id = ms.target_id AND me.active = 1)")
	if err != nil {
		return nil, fmt.Errorf("监控资格集查询失败: %w", err)
	}
	defer rows.Close()
	out := []int64{}
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			return nil, fmt.Errorf("监控资格集读取失败: %w", err)
		}
		out = append(out, id)
	}
	return out, rows.Err()
}

// gameSources 对象来源集（Python monitoring.sources_map，400/批）：
// map[appid] → 该对象全部激活来源名。
func gameSources(ctx context.Context, db *sql.DB, ids []int64) (map[int64]map[string]bool, error) {
	out := map[int64]map[string]bool{}
	for _, part := range chunkInt64s(ids, 400) {
		rows, err := db.QueryContext(ctx,
			"SELECT target_id, source FROM monitor_sources"+
				" WHERE target_type = 'game' AND target_id IN ("+placeholders(len(part))+") AND active = 1",
			int64Args(part)...)
		if err != nil {
			return nil, fmt.Errorf("监控来源查询失败: %w", err)
		}
		err = func() error {
			defer rows.Close()
			for rows.Next() {
				var id int64
				var source string
				if err := rows.Scan(&id, &source); err != nil {
					return fmt.Errorf("监控来源读取失败: %w", err)
				}
				if out[id] == nil {
					out[id] = map[string]bool{}
				}
				out[id][source] = true
			}
			return rows.Err()
		}()
		if err != nil {
			return nil, fmt.Errorf("监控来源遍历失败: %w", err)
		}
	}
	return out, nil
}

// excludedGameIDs 被移出对象集（Python monitor_targets.state 的 excluded 判定：
// 存在 active 排除行即 excluded；无 monitor_targets 行的对象不在此列）。
func excludedGameIDs(ctx context.Context, db *sql.DB, ids []int64) (map[int64]bool, error) {
	out := map[int64]bool{}
	for _, part := range chunkInt64s(ids, 400) {
		rows, err := db.QueryContext(ctx,
			"SELECT mt.target_id FROM monitor_targets mt"+
				" WHERE mt.target_type = 'game' AND mt.target_id IN ("+placeholders(len(part))+")"+
				" AND EXISTS (SELECT 1 FROM monitor_exclusions me"+
				" WHERE me.target_type = mt.target_type AND me.target_id = mt.target_id AND me.active = 1)",
			int64Args(part)...)
		if err != nil {
			return nil, fmt.Errorf("排除集查询失败: %w", err)
		}
		err = func() error {
			defer rows.Close()
			for rows.Next() {
				var id int64
				if err := rows.Scan(&id); err != nil {
					return fmt.Errorf("排除集读取失败: %w", err)
				}
				out[id] = true
			}
			return rows.Err()
		}()
		if err != nil {
			return nil, fmt.Errorf("排除集遍历失败: %w", err)
		}
	}
	return out, nil
}

// int64Args []int64 → SQL 参数切片。
func int64Args(ids []int64) []any {
	args := make([]any, len(ids))
	for i, id := range ids {
		args[i] = id
	}
	return args
}
