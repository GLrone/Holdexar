// lifecycle.go 关注/移除/恢复生命周期：全部走 monitoring 单一事实源，
// bundles 表零改动（对齐 Python bundles/service.py:635-708）。
package bundles

import (
	"context"
	"fmt"

	"holdexar/server-go/internal/monitoring"
)

// RemovalExclusionReason 移除链路挂的排除 reason：恢复时只解除本链路所挂
// 的排除，维护面板里用户显式设置的排除语义不被恢复动作洗掉。
const RemovalExclusionReason = "bundle_removed"

// ErrBundleNotFound 移除目标不在库（HTTP 层转 404，文案对齐 Python KeyError）。
type ErrBundleNotFound struct{ BundleID int64 }

func (e *ErrBundleNotFound) Error() string {
	return fmt.Sprintf("捆绑包 %d 不存在", e.BundleID)
}

func boolPtr(v bool) *bool { return &v }

// RemoveBundle 移除捆绑包：列表隐藏 + 退出刷新（包行与区域价保留）。
// 一次 Apply 同时摘掉全部用户来源（含星标/导入）并挂 bundle_removed 排除
// ——排除与释放双重门，整表刷新不会把它洗回来（Python 分两步写，终态同构）。
func (s *Service) RemoveBundle(ctx context.Context, bundleID int64) (map[string]any, error) {
	var exists bool
	if err := s.DB.QueryRowContext(ctx,
		"SELECT EXISTS(SELECT 1 FROM bundles WHERE bundle_id=?)", bundleID).Scan(&exists); err != nil {
		return nil, err
	}
	if !exists {
		return nil, &ErrBundleNotFound{BundleID: bundleID}
	}
	if _, err := s.Mon.Apply(ctx, "bundle", []int64{bundleID}, monitoring.Change{
		Sources: map[int64][]string{bundleID: {}}, Excluded: boolPtr(true), Reason: RemovalExclusionReason,
	}); err != nil {
		return nil, err
	}
	return map[string]any{"removed": true, "bundleId": bundleID}, nil
}

// RestoreBundle 恢复被移除的包：只对本链路所挂的排除生效，用户显式排除
// 保留。恢复走与重新导入同一条 track 通道（挂 import 来源 + 解除排除）——
// 移除已把目标置为无来源态，只解排除回不到刷新候选集。
func (s *Service) RestoreBundle(ctx context.Context, bundleID int64) (map[string]any, error) {
	reason, err := s.Mon.ExclusionReason(ctx, "bundle", bundleID)
	if err != nil {
		return nil, err
	}
	if reason != RemovalExclusionReason {
		return map[string]any{"restored": false, "bundleId": bundleID}, nil
	}
	if _, err := s.Mon.Apply(ctx, "bundle", []int64{bundleID}, monitoring.Change{
		Sources: map[int64][]string{bundleID: {"import"}}, Excluded: boolPtr(false),
	}); err != nil {
		return nil, err
	}
	return map[string]any{"restored": true, "bundleId": bundleID}, nil
}

// FollowBundle 关注一个包：挂 favorite 来源（不需要 Steam 账户），列表置顶
// 并进入刷新集。关注 = 用户显式要求盯住它：解除「别再爬它」后加来源，
// 其它来源不动（ensure 语义，只加不减）。
func (s *Service) FollowBundle(ctx context.Context, bundleID int64) (map[string]any, error) {
	if _, err := s.Mon.Apply(ctx, "bundle", []int64{bundleID}, monitoring.Change{
		Sources: map[int64][]string{bundleID: {"favorite"}}, AddOnly: true, Excluded: boolPtr(false),
	}); err != nil {
		return nil, err
	}
	return map[string]any{"bundleId": bundleID, "followed": true}, nil
}

// UnfollowBundle 取消关注：只摘 favorite 来源（导入来源与排除标不动）。
// 星标是唯一激活来源时，摘除后回到「无记录」基线（forget）——刷新集只收
// 监控层在册的包，无记录即不随轮刷新，价格停留在最后一次快照。
func (s *Service) UnfollowBundle(ctx context.Context, bundleID int64) (map[string]any, error) {
	sources, err := s.Mon.Sources(ctx, "bundle", []int64{bundleID})
	if err != nil {
		return nil, err
	}
	if _, err := s.Mon.Apply(ctx, "bundle", []int64{bundleID}, monitoring.Change{
		Sources: map[int64][]string{bundleID: {}}, Managed: []string{"favorite"},
	}); err != nil {
		return nil, err
	}
	if onlyFavorite(sources[bundleID]) {
		if _, err := s.Mon.Forget(ctx, "bundle", bundleID); err != nil {
			return nil, err
		}
	}
	return map[string]any{"bundleId": bundleID, "followed": false}, nil
}

// FollowedBundleIDs 当前关注的包 id（升序去重）。
func (s *Service) FollowedBundleIDs(ctx context.Context) ([]int64, error) {
	rows, err := s.DB.QueryContext(ctx,
		"SELECT DISTINCT target_id FROM monitor_sources WHERE target_type='bundle' AND source='favorite' AND active=1 ORDER BY target_id")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	ids := []int64{}
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

func onlyFavorite(sources []string) bool {
	return len(sources) == 1 && sources[0] == "favorite"
}
