package store

import (
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

func writeFixtureDB(t *testing.T, path string, version int) *sql.DB {
	t.Helper()
	writer, err := Open(path)
	if err != nil {
		t.Fatalf("打开夹具库失败: %v", err)
	}
	t.Cleanup(func() { writer.Close() })
	for _, stmt := range []string{
		"CREATE TABLE sample(appid INTEGER PRIMARY KEY, name TEXT)",
		"INSERT INTO sample VALUES (1, '只狼'), (2, '群星')",
		"CREATE VIRTUAL TABLE sample_fts USING fts5(name_bi)",
		"INSERT INTO sample_fts VALUES ('只狼 狼')",
		fmt.Sprintf("PRAGMA user_version = %d", version),
	} {
		if _, err := writer.Exec(stmt); err != nil {
			t.Fatalf("夹具语句 %q 执行失败: %v", stmt, err)
		}
	}
	return writer
}

func TestSchemaVersionReadAndGate(t *testing.T) {
	path := filepath.Join(t.TempDir(), "holdexar.db")
	writeFixtureDB(t, path, 34)

	if got, err := ReadSchemaVersion(path); err != nil || got != 34 {
		t.Fatalf("ReadSchemaVersion = (%d, %v)，期望 34", got, err)
	}
	if err := EnsureSupportedVersion(path, 34); err != nil {
		t.Fatalf("版本不超前应放行，实际报错: %v", err)
	}
	got, err := QuickCheck(path)
	if err != nil {
		t.Fatalf("QuickCheck 失败: %v", err)
	}
	if got != "ok" {
		t.Fatalf("QuickCheck = %q，期望 ok", got)
	}
}

func TestEnsureSupportedVersionRejectsNewer(t *testing.T) {
	path := filepath.Join(t.TempDir(), "holdexar.db")
	writeFixtureDB(t, path, 35)

	err := EnsureSupportedVersion(path, 34)
	if err == nil {
		t.Fatal("user_version=35 应被拒绝启动")
	}
	want := "数据库结构版本 v35 高于当前程序支持的数据库版本 v34，拒绝启动"
	if err.Error() != want {
		t.Fatalf("拒绝文案 = %q，期望 %q", err.Error(), want)
	}
}

func TestEnsureSupportedVersionRejectsOlder(t *testing.T) {
	path := filepath.Join(t.TempDir(), "holdexar.db")
	writeFixtureDB(t, path, 33)

	err := EnsureSupportedVersion(path, 34)
	if err == nil {
		t.Fatal("user_version=33 应被拒绝启动")
	}
	want := "数据库结构版本 v33 低于当前程序支持的数据库版本 v34，拒绝启动：本程序不含迁移链，请先由初始化链完成结构升级"
	if err.Error() != want {
		t.Fatalf("拒绝文案 = %q，期望 %q", err.Error(), want)
	}
}

func TestSchemaVersionMissingFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "absent.db")

	if _, err := ReadSchemaVersion(path); err == nil {
		t.Fatal("不存在的库文件应报错")
	}
	if err := EnsureSupportedVersion(path, 34); err == nil {
		t.Fatal("EnsureSupportedVersion 对缺失库应报错")
	}
	if _, err := QuickCheck(path); err == nil {
		t.Fatal("QuickCheck 对缺失库应报错")
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatal("只读打开不得创建库文件")
	}
	for _, side := range []string{path + "-wal", path + "-shm"} {
		if _, err := os.Stat(side); !os.IsNotExist(err) {
			t.Fatalf("只读打开不得创建侧车文件 %s", side)
		}
	}
}

func TestReadOnlySeesUncheckpointedWAL(t *testing.T) {
	path := filepath.Join(t.TempDir(), "holdexar.db")
	writeFixtureDB(t, path, 34)

	walInfo, err := os.Stat(path + "-wal")
	if err != nil || walInfo.Size() == 0 {
		t.Fatalf("WAL 侧车文件应存在且非空: %v", err)
	}

	main, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("主库文件读取失败: %v", err)
	}
	mainOnly := path + ".mainonly"
	if err := os.WriteFile(mainOnly, main, 0o600); err != nil {
		t.Fatalf("主库文件复制失败: %v", err)
	}
	if got, err := ReadSchemaVersion(mainOnly); err != nil || got != 0 {
		t.Fatalf("仅主库文件读出 user_version = (%d, %v)，期望 0（未 checkpoint）", got, err)
	}

	ro, err := openReadOnly(path)
	if err != nil {
		t.Fatalf("只读打开失败: %v", err)
	}
	defer ro.Close()
	if got, err := ReadSchemaVersion(path); err != nil || got != 34 {
		t.Fatalf("只读穿透 WAL 读 user_version = (%d, %v)，期望 34", got, err)
	}
	var nameBi string
	if err := ro.QueryRow("SELECT name_bi FROM sample_fts LIMIT 1").Scan(&nameBi); err != nil {
		t.Fatalf("只读连接读 WAL 未 checkpoint 的 FTS5 数据失败: %v", err)
	}
	if nameBi != "只狼 狼" {
		t.Fatalf("FTS5 行 = %q，期望 %q", nameBi, "只狼 狼")
	}
}
