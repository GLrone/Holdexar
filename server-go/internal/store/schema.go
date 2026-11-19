package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/url"
	"path/filepath"
	"strings"
)

// openReadOnly 以 mode=ro 且不设 busy_timeout 只读打开库，可直接读取未 checkpoint 的 WAL 侧车。
func openReadOnly(dbPath string) (*sql.DB, error) {
	abs, err := filepath.Abs(dbPath)
	if err != nil {
		return nil, fmt.Errorf("数据库路径解析失败: %w", err)
	}
	// file: URI 交由 SQLite 自身解析；Windows 盘符路径需以 / 起头
	uri := (&url.URL{Scheme: "file", Path: "/" + filepath.ToSlash(abs), RawQuery: "mode=ro"}).String()
	db, err := sql.Open(sqliteDriverName, uri)
	if err != nil {
		return nil, fmt.Errorf("数据库连接失败: %w", err)
	}
	return db, nil
}

func ReadSchemaVersion(dbPath string) (int, error) {
	db, err := openReadOnly(dbPath)
	if err != nil {
		return 0, fmt.Errorf("数据库结构版本无法读取，已停止选择程序: %w", err)
	}
	defer db.Close()
	var version int
	if err := db.QueryRow("PRAGMA user_version").Scan(&version); err != nil {
		return 0, fmt.Errorf("数据库结构版本无法读取，已停止选择程序: %w", err)
	}
	return version, nil
}

// EnsureSupportedVersion 严格校验库 user_version 等于 supported，高于或低于均拒绝。
func EnsureSupportedVersion(dbPath string, supported int) error {
	current, err := ReadSchemaVersion(dbPath)
	if err != nil {
		return err
	}
	if current > supported {
		return fmt.Errorf("数据库结构版本 v%d 高于当前程序支持的数据库版本 v%d，拒绝启动", current, supported)
	}
	if current < supported {
		return fmt.Errorf("数据库结构版本 v%d 低于当前程序支持的数据库版本 v%d，拒绝启动：本程序不含迁移链，请先由初始化链完成结构升级", current, supported)
	}
	return nil
}

func QuickCheck(dbPath string) (string, error) {
	db, err := openReadOnly(dbPath)
	if err != nil {
		return "", fmt.Errorf("完整性快检无法执行: %w", err)
	}
	defer db.Close()
	return CheckIntegrity(context.Background(), db, false)
}

func Snapshot(ctx context.Context, db *sql.DB, destination string) error {
	_, err := db.ExecContext(ctx, "VACUUM INTO ?", destination)
	return err
}

func CheckIntegrity(ctx context.Context, db *sql.DB, deep bool) (string, error) {
	query := "PRAGMA quick_check"
	if deep {
		query = "PRAGMA integrity_check"
	}
	rows, err := db.QueryContext(ctx, query)
	if err != nil {
		return "", fmt.Errorf("完整性快检无法执行: %w", err)
	}
	defer rows.Close()
	var lines []string
	for rows.Next() {
		var line string
		if err := rows.Scan(&line); err != nil {
			return "", fmt.Errorf("完整性快检无法执行: %w", err)
		}
		lines = append(lines, line)
	}
	if err := rows.Err(); err != nil {
		return "", fmt.Errorf("完整性快检无法执行: %w", err)
	}
	if len(lines) == 0 {
		return "", errors.New("完整性快检无结果")
	}
	return strings.Join(lines, "; "), nil
}
