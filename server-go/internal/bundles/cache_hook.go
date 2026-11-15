// cache_hook.go 派生联动注入点：bundles 排序快照在调用方事务连接上执行，
// 追踪区集自取（对齐 Python refresh_bundle_sort_cache 注入 session 路径——
// 不得再开自己的事务）。serverd 侧 derived.RefreshBundleSortCache = hook。
package bundles

import (
	"context"
	"database/sql"
	"sort"
)

// RefreshSortCacheHook derived.UpdateMemberBundles 的回调形态：受影响包的
// 排序快照在调用方 conn 上重建（tracked 取 crawl_regions 启用集）。
func (s *Service) RefreshSortCacheHook(ctx context.Context, conn *sql.Conn, bundleIDs []int64) error {
	trackedMap := trackedRegionCodes(ctx, s.DB)
	codes := make([]string, 0, len(trackedMap))
	for code := range trackedMap {
		codes = append(codes, code)
	}
	sort.Strings(codes)
	_, err := RefreshSortCache(ctx, conn, bundleIDs, codes)
	return err
}
