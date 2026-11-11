package migration

import (
	"context"
	"database/sql"
	"fmt"
	"path/filepath"
	"strings"
)

type migrationStep func(context.Context, *sql.Conn) error

func migrationExec(ctx context.Context, conn *sql.Conn, statements ...string) error {
	for _, statement := range statements {
		if _, err := conn.ExecContext(ctx, statement); err != nil {
			return err
		}
	}
	return nil
}

func migrationDataDir(ctx context.Context, conn *sql.Conn) (string, error) {
	rows, err := conn.QueryContext(ctx, "PRAGMA database_list")
	if err != nil {
		return "", err
	}
	defer rows.Close()
	for rows.Next() {
		var seq int
		var name, file string
		if err := rows.Scan(&seq, &name, &file); err != nil {
			return "", err
		}
		if name == "main" && file != "" {
			return filepath.Dir(file), nil
		}
	}
	if err := rows.Err(); err != nil {
		return "", err
	}
	return "", fmt.Errorf("数据库没有可用的数据目录")
}

func migrationColumns(ctx context.Context, conn *sql.Conn, table string) (map[string]bool, error) {
	rows, err := conn.QueryContext(ctx, "SELECT name FROM pragma_table_info(?)", table)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	columns := make(map[string]bool)
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			return nil, err
		}
		columns[name] = true
	}
	return columns, rows.Err()
}

func migrationTableExists(ctx context.Context, conn *sql.Conn, table string) (bool, error) {
	var count int
	err := conn.QueryRowContext(ctx, "SELECT count(*) FROM sqlite_schema WHERE type='table' AND name=?", table).Scan(&count)
	return count != 0, err
}

func migrationQuoteIdent(value string) string {
	return `"` + strings.ReplaceAll(value, `"`, `""`) + `"`
}
