package modulectl

import (
	"context"
	"database/sql"
	"errors"
	"strings"

	"holdexar/server-go/internal/store"
)

// Control 基于 app_settings 单键布尔控制模块开关（无键默认启用，无缓存直查）。
type Control struct {
	key string
}

func New(key string) *Control { return &Control{key: key} }

func (c *Control) Enabled(ctx context.Context, q interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
}) (bool, error) {
	var raw sql.NullString
	e := q.QueryRowContext(ctx, `SELECT value_json FROM app_settings WHERE key=?`, c.key).Scan(&raw)
	if errors.Is(e, sql.ErrNoRows) {
		return true, nil
	}
	if e != nil {
		return false, e
	}
	return strings.TrimSpace(raw.String) == "true", nil
}

func (c *Control) SetEnabled(ctx context.Context, db *sql.DB, gate *store.WriteGate, enabled bool) error {
	raw := "false"
	if enabled {
		raw = "true"
	}
	return gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		_, e := conn.ExecContext(ctx, `INSERT INTO app_settings(key,value_json,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)
			ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at`, c.key, raw)
		return e
	})
}
