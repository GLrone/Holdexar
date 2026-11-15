package monitoring

import (
	"context"
	"database/sql"

	"holdexar/server-go/internal/store"
)

// ExclusionReason 当前 active 排除的 reason；无 active 排除时返回空串
// （Python exclusion_reason）。供恢复类动作判定「这条排除是不是自己挂的」
// ——只解除本链路所挂的排除，用户显式设置的排除语义不被洗掉。
func (s *Service) ExclusionReason(ctx context.Context, targetType string, targetID int64) (string, error) {
	var reason sql.NullString
	if e := s.DB.QueryRowContext(ctx, `SELECT reason FROM monitor_exclusions WHERE target_type=? AND target_id=? AND active=1`, targetType, targetID).Scan(&reason); e != nil {
		if e == sql.ErrNoRows {
			return "", nil
		}
		return "", e
	}
	return reason.String, nil
}

// Forget 抹掉对象的监控记录（target 行 + 来源历史行），回到「无记录」基线
// （Python forget）。有激活来源或激活排除时拒绝执行（返回 false，不删）；
// 排除历史行保留（排除/解除可回溯）；无记录时同样返回 true。
func (s *Service) Forget(ctx context.Context, targetType string, targetID int64) (bool, error) {
	applied := false
	e := s.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		var activeSource, activeExclusion bool
		if e := conn.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM monitor_sources WHERE target_type=? AND target_id=? AND active=1)`, targetType, targetID).Scan(&activeSource); e != nil {
			return e
		}
		if e := conn.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM monitor_exclusions WHERE target_type=? AND target_id=? AND active=1)`, targetType, targetID).Scan(&activeExclusion); e != nil {
			return e
		}
		if activeSource || activeExclusion {
			return nil
		}
		if _, e := conn.ExecContext(ctx, `DELETE FROM monitor_sources WHERE target_type=? AND target_id=?`, targetType, targetID); e != nil {
			return e
		}
		if _, e := conn.ExecContext(ctx, `DELETE FROM monitor_targets WHERE target_type=? AND target_id=?`, targetType, targetID); e != nil {
			return e
		}
		applied = true
		return nil
	})
	if e != nil {
		return false, e
	}
	return applied, nil
}

// ActiveBundleIDs 捆绑包 crawl 资格集（Python active_ids("bundle")）：激活
// 来源且未被排除的对象，MAX(priority) 降序、target_id 升序排队。
func (s *Service) ActiveBundleIDs(ctx context.Context) ([]int64, error) {
	rows, e := s.DB.QueryContext(ctx, `SELECT ms.target_id FROM monitor_sources ms
 JOIN monitor_targets mt ON mt.target_type=ms.target_type AND mt.target_id=ms.target_id
 WHERE ms.target_type='bundle' AND ms.active=1
 AND NOT EXISTS(SELECT 1 FROM monitor_exclusions me WHERE me.target_type=ms.target_type AND me.target_id=ms.target_id AND me.active=1)
 GROUP BY ms.target_id ORDER BY MAX(ms.priority) DESC, ms.target_id`)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	out := []int64{}
	for rows.Next() {
		var id int64
		if e := rows.Scan(&id); e != nil {
			return nil, e
		}
		out = append(out, id)
	}
	return out, rows.Err()
}
