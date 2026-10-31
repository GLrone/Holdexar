// Package store 提供 SQLite 存储引擎与单写者写闸。
package store

import (
	"context"
	"database/sql"
	"fmt"
	"net/url"
	"path/filepath"
	"strings"
)

const sqliteDriverName = "sqlite"

// enginePragmas 每连接应用的 PRAGMA（生产同款参数）。
// modernc 经 DSN _pragma 逐连接执行，并自动把 busy_timeout 置于最前。
var enginePragmas = []string{
	"busy_timeout(60000)", // 等锁 60s：扛过种子合并等长持锁窗口
	"journal_mode(WAL)",
	"synchronous(NORMAL)",          // 保事务一致，断电可能丢最近提交
	"journal_size_limit(67108864)", // WAL 超 64MB 即截，防峰值滞留
	"foreign_keys(ON)",
	"cache_size(-262144)", // 256MB 页缓存
	"mmap_size(0)",        // mmap 按连接各映射一份，不启用
	"temp_store(MEMORY)",
}

// Open 打开 SQLite 库，返回已应用连接级 PRAGMA 的连接池。
func Open(path string) (*sql.DB, error) {
	abs, err := filepath.Abs(path)
	if err != nil {
		return nil, fmt.Errorf("数据库路径解析失败: %w", err)
	}
	// file: URI 交由 SQLite 自身解析；Windows 盘符路径需以 / 起头
	p := filepath.ToSlash(abs)
	if !strings.HasPrefix(p, "/") {
		p = "/" + p
	}
	uri := url.URL{Scheme: "file", Path: p}
	q := url.Values{}
	for _, pragma := range enginePragmas {
		q.Add("_pragma", pragma)
	}
	db, err := sql.Open(sqliteDriverName, uri.String()+"?"+q.Encode())
	if err != nil {
		return nil, fmt.Errorf("数据库打开失败: %w", err)
	}
	if err := db.PingContext(context.Background()); err != nil {
		db.Close()
		return nil, fmt.Errorf("数据库连接失败: %w", err)
	}
	return db, nil
}
