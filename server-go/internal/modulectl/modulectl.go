package modulectl

import (
	"context"
	"database/sql"
	"errors"
	"strings"

	"holdexar/server-go/internal/store"
)

// 内部模块开关：app_settings 单键布尔（与 Epic 模块键同形）。停用语义由
// 宿主在执行入口判定——拒新执行、在途一次自然收尾；本包只管读取与落库，
// 不拥有调度或取消。默认启用（无键 = 模块在役）。读路径直查主键，无缓存。
type Control struct {
	key string
}

// New 构造模块开关控制器。
func New(key string) *Control { return &Control{key: key} }

// Enabled 读当前开关（库行缺失 = 启用）。
func (c *Control) Enabled(ctx context.Context, q interface{ QueryRowContext(context.Context, string, ...any) *sql.Row }) (bool, error) {
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

// SetEnabled 经写闸落库。
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
