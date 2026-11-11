package store

import (
	"context"
	"database/sql"
	_ "embed"
	"fmt"
	"strings"
	"time"
)

//go:embed bootstrap_schema.sql
var bootstrapSchema string

//go:embed bootstrap_seeds.sql
var bootstrapSeeds string

// InitializeFreshDatabase creates the current application schema only in an empty database.
func InitializeFreshDatabase(ctx context.Context, db *sql.DB) error {
	conn, err := db.Conn(ctx)
	if err != nil {
		return fmt.Errorf("reserve database initialization connection: %w", err)
	}
	defer conn.Close()
	if _, err := conn.ExecContext(ctx, "BEGIN IMMEDIATE"); err != nil {
		return fmt.Errorf("begin database initialization: %w", err)
	}
	committed := false
	defer func() {
		if !committed {
			_, _ = conn.ExecContext(context.Background(), "ROLLBACK")
		}
	}()

	var version, objects int
	if err := conn.QueryRowContext(ctx, "PRAGMA user_version").Scan(&version); err != nil {
		return fmt.Errorf("read database initialization version: %w", err)
	}
	if err := conn.QueryRowContext(ctx, "SELECT count(*) FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'").Scan(&objects); err != nil {
		return fmt.Errorf("inspect database before initialization: %w", err)
	}
	if version != 0 || objects != 0 {
		return fmt.Errorf("fresh database initialization requires an empty unversioned database (version=%d, objects=%d)", version, objects)
	}
	if _, err := conn.ExecContext(ctx, bootstrapSchema); err != nil {
		return fmt.Errorf("create application schema: %w", err)
	}
	now := time.Now().In(time.FixedZone("Asia/Shanghai", 8*60*60))
	seeds := strings.NewReplacer(
		"{{bootstrap_time}}", now.Format("2006-01-02 15:04:05.000000"),
		"{{bootstrap_day}}", now.Format("2006-01-02"),
	).Replace(bootstrapSeeds)
	if _, err := conn.ExecContext(ctx, seeds); err != nil {
		return fmt.Errorf("insert application seed data: %w", err)
	}
	rows, err := conn.QueryContext(ctx, "PRAGMA foreign_key_check")
	if err != nil {
		return fmt.Errorf("check initialized foreign keys: %w", err)
	}
	invalid := rows.Next()
	rowErr := rows.Err()
	rows.Close()
	if rowErr != nil {
		return fmt.Errorf("check initialized foreign keys: %w", rowErr)
	}
	if invalid {
		return fmt.Errorf("initialized database contains foreign key violations")
	}
	if _, err := conn.ExecContext(ctx, fmt.Sprintf("PRAGMA user_version = %d", CurrentSchemaVersion)); err != nil {
		return fmt.Errorf("record initialized schema version: %w", err)
	}
	if _, err := conn.ExecContext(ctx, "COMMIT"); err != nil {
		return fmt.Errorf("commit database initialization: %w", err)
	}
	committed = true
	return nil
}
