package migration

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"time"

	"holdexar/server-go/internal/store"
)

var migrationTableDeclaration = regexp.MustCompile(`(?i)^CREATE TABLE\s+("?[a-z_][a-z_0-9]*"?)\s*\(`)
var migrationIndexDeclaration = regexp.MustCompile(`(?i)^CREATE (?:UNIQUE )?INDEX\s+(?:"?[a-z_][a-z_0-9]*"?)\s+ON\s+("?[a-z_][a-z_0-9]*"?)\s*\(`)

func migrationSchemaStatements() []string {
	// shortcut: the embedded schema contains only top-level CREATE declarations.
	parts := strings.Split(strings.ReplaceAll(strings.TrimSpace(store.CurrentSchemaSQL()), "\r\n", "\n"), "\nCREATE ")
	for i := range parts {
		if i != 0 {
			parts[i] = "CREATE " + parts[i]
		}
		parts[i] = strings.TrimSpace(parts[i])
	}
	return parts
}

func migrationTransaction(ctx context.Context, db *sql.DB, body migrationStep) error {
	conn, err := db.Conn(ctx)
	if err != nil {
		return err
	}
	defer conn.Close()
	if err := migrationExec(ctx, conn, "BEGIN IMMEDIATE"); err != nil {
		return err
	}
	defer conn.ExecContext(context.Background(), "ROLLBACK")
	if err := body(ctx, conn); err != nil {
		return err
	}
	return migrationExec(ctx, conn, "COMMIT")
}

func prepareExistingSchema(ctx context.Context, conn *sql.Conn, version int) error {
	newTables := map[string]bool{}
	for _, statement := range migrationSchemaStatements() {
		match := migrationTableDeclaration.FindStringSubmatch(statement)
		if match == nil {
			continue
		}
		name := strings.Trim(match[1], `"`)
		exists, err := migrationTableExists(ctx, conn, name)
		if err != nil {
			return err
		}
		if !exists {
			if err := migrationExec(ctx, conn, statement); err != nil {
				return err
			}
			newTables[name] = true
		}
	}
	if version < 10 {
		for _, column := range []struct{ table, name, definition string }{
			{"tracked_accounts", "item_count", "INTEGER DEFAULT 0"},
			{"monitor_targets", "state", "VARCHAR(16) DEFAULT 'released'"},
			{"monitor_targets", "priority", "INTEGER DEFAULT 0"},
		} {
			columns, err := migrationColumns(ctx, conn, column.table)
			if err != nil {
				return err
			}
			if !columns[column.name] {
				if err := migrationExec(ctx, conn, "ALTER TABLE "+migrationQuoteIdent(column.table)+" ADD COLUMN "+migrationQuoteIdent(column.name)+" "+column.definition); err != nil {
					return err
				}
			}
		}
	}
	for _, column := range migrationExtraColumns {
		columns, err := migrationColumns(ctx, conn, column.table)
		if err != nil {
			return err
		}
		if len(columns) > 0 && !columns[column.name] {
			if err := migrationExec(ctx, conn, "ALTER TABLE "+migrationQuoteIdent(column.table)+" ADD COLUMN "+migrationQuoteIdent(column.name)+" "+column.definition); err != nil {
				return err
			}
		}
	}
	for _, statement := range migrationSchemaStatements() {
		match := migrationIndexDeclaration.FindStringSubmatch(statement)
		if match != nil && newTables[strings.Trim(match[1], `"`)] {
			if err := migrationExec(ctx, conn, statement); err != nil {
				return err
			}
		}
	}
	return migrationExec(ctx, conn, migrationExtraIndexes...)
}

func migrationSteps() (map[int]migrationStep, error) {
	steps := map[int]migrationStep{}
	for _, group := range []map[int]migrationStep{earlyMigrationSteps, middleMigrationSteps, lateMigrationSteps} {
		for version, step := range group {
			if steps[version] != nil || step == nil {
				return nil, fmt.Errorf("数据库升级步骤重复或为空: v%d", version)
			}
			steps[version] = step
		}
	}
	for version := 2; version <= store.CurrentSchemaVersion; version++ {
		if steps[version] == nil {
			return nil, fmt.Errorf("数据库升级步骤缺失: v%d", version)
		}
	}
	return steps, nil
}

func upgradeDatabase(ctx context.Context, db *sql.DB, version int) error {
	steps, err := migrationSteps()
	if err != nil {
		return err
	}
	if err := migrationTransaction(ctx, db, func(ctx context.Context, conn *sql.Conn) error { return prepareExistingSchema(ctx, conn, version) }); err != nil {
		return fmt.Errorf("旧库结构准备失败，已保留升级前快照: %w", err)
	}
	if version == 0 {
		if err := migrationTransaction(ctx, db, func(ctx context.Context, conn *sql.Conn) error {
			return migrationExec(ctx, conn, "PRAGMA user_version=1")
		}); err != nil {
			return err
		}
		version = 1
	}
	for target := version + 1; target <= store.CurrentSchemaVersion; target++ {
		if err := migrationTransaction(ctx, db, func(ctx context.Context, conn *sql.Conn) error {
			var live int
			if err := conn.QueryRowContext(ctx, "PRAGMA user_version").Scan(&live); err != nil {
				return err
			}
			if live != target-1 {
				return fmt.Errorf("数据库版本在升级前发生变化: v%d", live)
			}
			if err := steps[target](ctx, conn); err != nil {
				return err
			}
			return migrationExec(ctx, conn, fmt.Sprintf("PRAGMA user_version=%d", target))
		}); err != nil {
			return fmt.Errorf("数据库v%d升级失败，当前步骤已回滚: %w", target, err)
		}
	}
	return migrationTransaction(ctx, db, ensureCurrentSeedData)
}

func ensureCurrentSeedData(ctx context.Context, conn *sql.Conn) error {
	now := time.Now().In(time.FixedZone("Asia/Shanghai", 8*3600))
	sqlText := strings.NewReplacer("{{bootstrap_time}}", now.Format("2006-01-02 15:04:05.000000"), "{{bootstrap_day}}", now.Format("2006-01-02")).Replace(strings.ReplaceAll(store.CurrentSeedSQL(), "\r\n", "\n"))
	parts := strings.Split(strings.TrimSpace(sqlText), "\n\n")
	for _, part := range parts {
		statement := strings.TrimSuffix(strings.TrimSpace(part), ";")
		switch {
		case strings.HasPrefix(statement, `INSERT INTO "crawl_regions"`):
			statement += ` ON CONFLICT(code) DO UPDATE SET name=excluded.name,currency=excluded.currency,updated_at=excluded.updated_at WHERE crawl_regions.name IS NOT excluded.name OR crawl_regions.currency IS NOT excluded.currency`
		case strings.HasPrefix(statement, `INSERT INTO "tags"`):
			statement += ` ON CONFLICT(tagid) DO UPDATE SET name_en=excluded.name_en,updated_at=excluded.updated_at WHERE tags.name_en IS NULL AND excluded.name_en IS NOT NULL AND excluded.name_en<>''`
		case strings.HasPrefix(statement, `INSERT INTO "fx_rates"`), strings.HasPrefix(statement, `INSERT INTO "app_settings"`):
			statement = strings.Replace(statement, "INSERT INTO", "INSERT OR IGNORE INTO", 1)
		default:
			return fmt.Errorf("数据库初始数据声明无法识别")
		}
		if err := migrationExec(ctx, conn, statement); err != nil {
			return err
		}
	}
	var legacy sql.NullString
	err := conn.QueryRowContext(ctx, "SELECT CAST(value_json AS TEXT) FROM app_settings WHERE key='crawl.enabled_regions'").Scan(&legacy)
	if err == sql.ErrNoRows {
		return nil
	}
	if err != nil {
		return err
	}
	if legacy.Valid {
		value, err := migrationDecodeJSON(legacy.String)
		if err != nil {
			return err
		}
		if items, ok := value.([]any); ok && len(items) > 0 {
			wanted := make([]string, 0, len(items))
			for _, item := range items {
				wanted = append(wanted, strings.ToLower(migrationString(item)))
			}
			encoded, err := json.Marshal(wanted)
			if err != nil {
				return err
			}
			if _, err := conn.ExecContext(ctx, "UPDATE crawl_regions SET enabled=EXISTS(SELECT 1 FROM json_each(?) WHERE value=crawl_regions.code)", string(encoded)); err != nil {
				return err
			}
		}
	}
	return migrationExec(ctx, conn, "DELETE FROM app_settings WHERE key='crawl.enabled_regions'")
}
