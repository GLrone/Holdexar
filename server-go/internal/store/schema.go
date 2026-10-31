package store

import (
	"database/sql"
	"errors"
	"fmt"
	"net/url"
	"path/filepath"
	"strings"
)

// openReadOnly 只读打开存量库：mode=ro 下文件不存在即报错、不建新文件，
// 连接不得写。WAL 模式库的 -wal/-shm 侧车由 SQLite 随主文件自行发现，
// 只读连接以只读方式复用侧车中未 checkpoint 的内容，不触发落盘改动。
// 不设 busy_timeout：驱动缺省即 SQLite 内建 0，锁被占立即失败（对齐
// Python 只读通道 timeout=0）。
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

// ReadSchemaVersion 只读读取库结构版本（PRAGMA user_version），经 WAL
// 侧车取最新已提交值；版本存于主库第 1 页，未 checkpoint 时以侧车为准。
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

// EnsureSupportedVersion 拒绝结构版本超前的存量库；版本账本只拦超前，
// 落后版本交由迁移链推进。
func EnsureSupportedVersion(dbPath string, supported int) error {
	current, err := ReadSchemaVersion(dbPath)
	if err != nil {
		return err
	}
	if current > supported {
		return fmt.Errorf("数据库结构版本 v%d 高于当前程序支持的数据库版本 v%d，拒绝启动", current, supported)
	}
	return nil
}

// QuickCheck 只读完整性快检（PRAGMA quick_check）：完好库返回 "ok"，
// 损伤库返回损伤描述（首 100 条，分号连接）。
func QuickCheck(dbPath string) (string, error) {
	db, err := openReadOnly(dbPath)
	if err != nil {
		return "", fmt.Errorf("完整性快检无法执行: %w", err)
	}
	defer db.Close()
	rows, err := db.Query("PRAGMA quick_check")
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
