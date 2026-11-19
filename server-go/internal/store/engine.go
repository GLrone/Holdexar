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

// enginePragmas 由 modernc 驱动经 DSN _pragma 逐连接执行，且 busy_timeout 会被优先应用。
var enginePragmas = []string{
	"busy_timeout(60000)",
	"journal_mode(WAL)",
	"synchronous(NORMAL)", // 保事务一致，断电可能丢最近提交
	"journal_size_limit(67108864)",
	"foreign_keys(ON)",
	"cache_size(-262144)",
	"mmap_size(0)", // mmap 按连接各映射一份，不启用
	"temp_store(MEMORY)",
}

func Open(path string) (*sql.DB, error) {
	abs, err := filepath.Abs(path)
	if err != nil {
		return nil, fmt.Errorf("数据库路径解析失败: %w", err)
	}
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
