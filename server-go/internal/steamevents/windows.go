package steamevents

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"sync/atomic"
	"time"

	"holdexar/server-go/internal/crawler/dbwriter"
	"holdexar/server-go/internal/store"
)

const backfillWatermarkKey = "steam_events.backfill_watermark"

type eventWindow struct {
	key   string
	start string
	end   string // exclusive
}

type Windows struct {
	db     *sql.DB
	gate   *store.WriteGate
	now    func() time.Time
	loaded atomic.Pointer[[]eventWindow]
}

func NewWindows(db *sql.DB, gate *store.WriteGate, now func() time.Time) *Windows {
	if now == nil {
		now = time.Now
	}
	return &Windows{db: db, gate: gate, now: now}
}

func (w *Windows) Reload(ctx context.Context) error {
	rows, err := w.db.QueryContext(ctx, `SELECT event_key, date(start_date), date(end_date, '+1 day')
		FROM steam_events
		ORDER BY CASE category WHEN 'seasonal_sale' THEN 0 WHEN 'next_fest' THEN 1 WHEN 'themed_fest' THEN 2 ELSE 9 END,
		start_date, id`)
	if err != nil {
		return err
	}
	defer rows.Close()
	var windows []eventWindow
	for rows.Next() {
		var window eventWindow
		if err := rows.Scan(&window.key, &window.start, &window.end); err != nil {
			return err
		}
		windows = append(windows, window)
	}
	if err := rows.Err(); err != nil {
		return err
	}
	w.loaded.Store(&windows)
	return nil
}

func (w *Windows) KeyAt(ts time.Time) *string {
	windows := w.loaded.Load()
	if windows == nil {
		return nil
	}
	day := ts.In(dbwriter.BeijingLoc).Format(time.DateOnly)
	for _, window := range *windows {
		if day >= window.start && day < window.end {
			return &window.key
		}
	}
	return nil
}

// Backfill tags missing observations from the calendar loaded by Reload.
func (w *Windows) Backfill(ctx context.Context, keys []string) (int, error) {
	if len(keys) == 0 {
		return 0, nil
	}
	windows := w.loaded.Load()
	if windows == nil {
		return 0, errors.New("Steam event windows have not been loaded")
	}
	selected := make(map[string]bool, len(keys))
	for _, key := range keys {
		selected[key] = true
	}
	now := w.now().In(dbwriter.BeijingLoc)
	stamp := dbwriter.FormatNaive(now.Add(-time.Hour))
	total := 0
	for i, window := range *windows {
		if !selected[window.key] {
			continue
		}
		var tagged int64
		err := w.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
			marks, err := loadBackfillMarks(ctx, conn)
			if err != nil {
				return err
			}
			const midnight = " 00:00:00"
			start, end := window.start+midnight, window.end+midnight
			var previous string
			if json.Unmarshal(marks[window.key], &previous) == nil && previous != "" {
				start = max(start, min(previous, stamp))
			}
			if start < end {
				query := `UPDATE game_price_history SET steam_event_key = ?
					WHERE steam_event_key IS NULL AND snapshot_at >= ? AND snapshot_at < ?`
				args := []any{window.key, start, end}
				for _, higher := range (*windows)[:i] {
					higherStart, higherEnd := higher.start+midnight, higher.end+midnight
					if higherStart < end && higherEnd > start {
						query += ` AND (snapshot_at < ? OR snapshot_at >= ?)`
						args = append(args, higherStart, higherEnd)
					}
				}
				result, err := conn.ExecContext(ctx, query, args...)
				if err != nil {
					return err
				}
				tagged, err = result.RowsAffected()
				if err != nil {
					return err
				}
			}
			marks[window.key] = json.RawMessage(strconv.Quote(stamp))
			return writeBackfillMarks(ctx, conn, marks, dbwriter.FormatNaive(now))
		})
		if err != nil {
			return total, fmt.Errorf("backfill Steam event %s: %w", window.key, err)
		}
		total += int(tagged)
	}
	return total, nil
}

// InvalidateBackfill clears revised windows' marks in the calendar write transaction.
func InvalidateBackfill(ctx context.Context, conn *sql.Conn, keys []string) error {
	if len(keys) == 0 {
		return nil
	}
	marks, err := loadBackfillMarks(ctx, conn)
	if err != nil {
		return err
	}
	before := len(marks)
	if before == 0 {
		return nil
	}
	encoded, err := json.Marshal(keys)
	if err != nil {
		return err
	}
	// An old overlap can expose untagged history once the calendar changes.
	rows, err := conn.QueryContext(ctx, `SELECT DISTINCT affected.event_key
		FROM json_each(?) AS revised JOIN steam_events AS old ON old.event_key = revised.value
		JOIN steam_events AS affected ON affected.event_key = old.event_key
		OR (affected.start_date <= old.end_date AND affected.end_date >= old.start_date)`, string(encoded))
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var key string
		if err := rows.Scan(&key); err != nil {
			return err
		}
		delete(marks, key)
	}
	if err := rows.Err(); err != nil {
		return err
	}
	if before == len(marks) {
		return nil
	}
	return writeBackfillMarks(ctx, conn, marks, dbwriter.FormatNaive(time.Now().In(dbwriter.BeijingLoc)))
}

func loadBackfillMarks(ctx context.Context, conn *sql.Conn) (map[string]json.RawMessage, error) {
	var raw sql.NullString
	err := conn.QueryRowContext(ctx, `SELECT value_json FROM app_settings WHERE key = ?`, backfillWatermarkKey).Scan(&raw)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return nil, err
	}
	marks := make(map[string]json.RawMessage)
	if raw.Valid {
		var decoded map[string]json.RawMessage
		if json.Unmarshal([]byte(raw.String), &decoded) == nil && decoded != nil {
			marks = decoded
		}
	}
	return marks, nil
}

func writeBackfillMarks(ctx context.Context, conn *sql.Conn, marks map[string]json.RawMessage, stamp string) error {
	encoded, err := json.Marshal(marks)
	if err != nil {
		return err
	}
	_, err = conn.ExecContext(ctx, `INSERT INTO app_settings (key, value_json, updated_at) VALUES (?, ?, ?)
		ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at`,
		backfillWatermarkKey, string(encoded), stamp)
	return err
}
