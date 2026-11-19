package monitoring

import (
	"context"
	"database/sql"

	"holdexar/server-go/internal/store"
)

// ExclusionReason 返回当前 active 排除原因，供恢复链路仅解除自建排除而不误清用户排除。
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

// Forget 清除无激活来源/排除对象的目标与来源记录，保留排除历史行；无记录返回 true。
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
