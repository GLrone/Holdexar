package store

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"fmt"
	"os"
	"time"
)

func (g *WriteGate) TruncateWAL(ctx context.Context, dbPath string) (map[string]any, error) {
	wait, cancel := context.WithTimeout(ctx, 250*time.Millisecond)
	lease, err := g.Acquire(wait, PriorityBackground)
	cancel()
	if err != nil {
		if errors.Is(err, context.DeadlineExceeded) && ctx.Err() == nil {
			return map[string]any{"skipped": "数据库正在写入"}, nil
		}
		return nil, err
	}
	defer lease.Done()
	conn, err := g.db.Conn(ctx)
	if err != nil {
		return nil, err
	}
	defer conn.Close()
	path := dbPath + "-wal"
	before, err := walSize(path)
	if err != nil {
		return nil, err
	}
	busy, frames, checkpointed, err := truncateCheckpoint(ctx, conn)
	if err != nil {
		return nil, err
	}
	after, err := walSize(path)
	if err != nil {
		return nil, err
	}
	return map[string]any{"beforeBytes": before, "afterBytes": after, "busy": busy != 0,
		"frames": frames, "checkpointedFrames": checkpointed}, nil
}

func truncateCheckpoint(ctx context.Context, conn *sql.Conn) (busy, frames, checkpointed int, err error) {
	var timeout int
	if err = conn.QueryRowContext(ctx, `PRAGMA busy_timeout`).Scan(&timeout); err != nil {
		return
	}
	defer func() {
		reset, cancel := context.WithTimeout(context.WithoutCancel(ctx), time.Second)
		defer cancel()
		if _, restoreErr := conn.ExecContext(reset, fmt.Sprintf(`PRAGMA busy_timeout=%d`, timeout)); restoreErr != nil {
			_ = conn.Raw(func(any) error { return driver.ErrBadConn })
			err = errors.Join(err, restoreErr)
		}
	}()
	// A checkpoint must yield immediately to another SQLite reader or writer.
	if _, err = conn.ExecContext(ctx, `PRAGMA busy_timeout=0`); err != nil {
		return
	}
	err = conn.QueryRowContext(ctx, `PRAGMA wal_checkpoint(TRUNCATE)`).Scan(&busy, &frames, &checkpointed)
	return
}

func walSize(path string) (int64, error) {
	info, err := os.Stat(path)
	if errors.Is(err, os.ErrNotExist) {
		return 0, nil
	}
	if err != nil {
		return 0, err
	}
	return info.Size(), nil
}
