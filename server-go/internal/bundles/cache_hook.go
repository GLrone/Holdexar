package bundles

import (
	"context"
	"database/sql"
	"slices"
)

// RefreshSortCacheHook 在调用方事务连接上重建受影响捆绑包的排序快照。
func (s *Service) RefreshSortCacheHook(ctx context.Context, conn *sql.Conn, bundleIDs []int64) error {
	trackedMap := trackedRegionCodes(ctx, s.DB)
	codes := make([]string, 0, len(trackedMap))
	for code := range trackedMap {
		codes = append(codes, code)
	}
	slices.Sort(codes)
	_, err := RefreshSortCache(ctx, conn, bundleIDs, codes)
	return err
}
