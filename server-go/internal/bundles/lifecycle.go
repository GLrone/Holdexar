package bundles

import (
	"context"
	"fmt"

	"holdexar/server-go/internal/monitoring"
)

// RemovalExclusionReason 本链路挂的排除 reason，恢复时只解除该 reason 以保留用户显式排除。
const RemovalExclusionReason = "bundle_removed"

type ErrBundleNotFound struct{ BundleID int64 }

func (e *ErrBundleNotFound) Error() string {
	return fmt.Sprintf("捆绑包 %d 不存在", e.BundleID)
}

func boolPtr(v bool) *bool { return &v }

// RemoveBundle 摘除用户来源并挂 bundle_removed 排除，防止整表刷新重新拉回。
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

// RestoreBundle 仅解除 bundle_removed 排除并回挂 import 来源（移除态无来源，仅解排除无法恢复刷新）。
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

func (s *Service) FollowBundle(ctx context.Context, bundleID int64) (map[string]any, error) {
	if _, err := s.Mon.Apply(ctx, "bundle", []int64{bundleID}, monitoring.Change{
		Sources: map[int64][]string{bundleID: {"favorite"}}, AddOnly: true, Excluded: boolPtr(false),
	}); err != nil {
		return nil, err
	}
	return map[string]any{"bundleId": bundleID, "followed": true}, nil
}

// UnfollowBundle 仅摘除 favorite 来源；若它是唯一激活来源则调用 Forget 退出监控刷新集。
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
