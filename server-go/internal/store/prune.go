package store

import (
	"context"
	"database/sql"
)

// PruneBatches commits each limited deletion separately. The last SQL argument is the batch size.
func (g *WriteGate) PruneBatches(ctx context.Context, query string, chunk, maxChunks int, args ...any) (int64, bool, error) {
	args = append(args, chunk)
	var total int64
	for range maxChunks {
		var count int64
		err := g.WithWrite(ctx, PriorityBackground, func(conn *sql.Conn) error {
			result, err := conn.ExecContext(ctx, query, args...)
			if err != nil {
				return err
			}
			count, err = result.RowsAffected()
			return err
		})
		if err != nil {
			return total, false, err
		}
		total += count
		if count < int64(chunk) {
			return total, false, nil
		}
	}
	return total, true, nil
}
