package notifications

import (
	"context"
	"database/sql"

	"holdexar/server-go/internal/store"
)

type RetentionResult struct {
	Recovered int64 `json:"recovered"`
	Deleted   int64 `json:"deleted"`
	Truncated bool  `json:"truncated"`
}

func (s *DeliveryService) PruneCandidates(ctx context.Context) (RetentionResult, error) {
	return s.pruneCandidates(ctx, 500, 20)
}

func (s *DeliveryService) pruneCandidates(ctx context.Context, chunk, maxChunks int) (RetentionResult, error) {
	now := s.options.Now()
	var recovered int64
	err := s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		var err error
		recovered, err = recoverStuckSending(ctx, conn, now)
		return err
	})
	result := RetentionResult{}
	if err != nil {
		return result, err
	}
	result.Recovered = recovered
	cutoff := deliveryStamp(now.AddDate(0, 0, -365))
	var cursor int64
	for range maxChunks {
		var scanned int
		var removed int64
		err := s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
			rows, err := conn.QueryContext(ctx, `SELECT id,status,attempts,last_error FROM notification_candidates
				WHERE id>? AND COALESCE(updated_at,created_at)<? AND status IN('delivered','unknown','failed') ORDER BY id LIMIT ?`, cursor, cutoff, chunk)
			if err != nil {
				return err
			}
			defer rows.Close()
			var ids []int64
			for rows.Next() {
				var status string
				var attempts sql.NullInt64
				var lastError sql.NullString
				if err := rows.Scan(&cursor, &status, &attempts, &lastError); err != nil {
					return err
				}
				scanned++
				if status != "failed" || attempts.Int64 >= maxAttempts || !isRetryable(lastError.String) {
					ids = append(ids, cursor)
				}
			}
			if err := rows.Err(); err != nil || len(ids) == 0 {
				return err
			}
			raw, err := deliveryJSON(ids)
			if err != nil {
				return err
			}
			deleted, err := conn.ExecContext(ctx, `DELETE FROM notification_candidates WHERE id IN (SELECT value FROM json_each(?))`, raw)
			if err != nil {
				return err
			}
			removed, err = deleted.RowsAffected()
			return err
		})
		if err != nil {
			return result, err
		}
		result.Deleted += removed
		if scanned < chunk {
			return result, nil
		}
	}
	result.Truncated = true
	return result, nil
}
