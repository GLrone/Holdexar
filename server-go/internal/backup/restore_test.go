package backup

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"holdexar/server-go/internal/migration"
	"holdexar/server-go/internal/store"
)

func restoreFixture(t *testing.T) (*Service, *sql.DB, string) {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "holdexar.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	// 真实 v34 全量结构+种子：恢复后的库要经得起启动校验
	if err := store.InitializeFreshDatabase(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO games(appid, name, family_sharing, trading_cards, is_epic, is_hb, hl_flag, pp_flag,
			diff_fen, is_adult, is_visual_novel, removed_strikes, positive_reviews, review_count, view_count)
		VALUES(620, 'Portal 2', 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)`); err != nil {
		t.Fatal(err)
	}
	// 主文件落全已提交内容：后续按字节复制的夹具才与真实库一致
	if _, err := db.Exec("PRAGMA wal_checkpoint(TRUNCATE)"); err != nil {
		t.Fatal(err)
	}
	gate := store.NewWriteGate(db, path)
	s := New(db, path, time.Now, gate)
	t.Cleanup(func() { db.Close() })
	return s, db, path
}

func TestRestoreRestoresCommittedStateAndRequestsRestart(t *testing.T) {
	s, db, path := restoreFixture(t)
	created, err := s.Create(context.Background(), "")
	if err != nil {
		t.Fatal(err)
	}
	// 备份后再写一笔：恢复应回到备份时点，这一行必须消失
	if _, err := db.Exec(`INSERT INTO games(appid, name, family_sharing, trading_cards, is_epic, is_hb, hl_flag, pp_flag,
			diff_fen, is_adult, is_visual_novel, removed_strikes, positive_reviews, review_count, view_count)
		VALUES(440, 'HL2', 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)`); err != nil {
		t.Fatal(err)
	}
	result, err := s.Restore(context.Background(), created.Name)
	if err != nil {
		t.Fatalf("restore: %v", err)
	}
	if !result.Restored || !result.Restarting || result.RolledBack {
		t.Fatalf("result=%+v", result)
	}
	db.Close()

	reopened, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	var count int
	if err := reopened.QueryRow("SELECT COUNT(*) FROM games").Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 1 {
		t.Fatalf("恢复后应只有备份时点的 1 行，实得 %d", count)
	}
	// 恢复保护标记待下次启动校验后清除
	marker, err := os.ReadFile(filepath.Join(filepath.Dir(path), "backups", ".restore.lock"))
	if err != nil {
		t.Fatal("恢复后保护标记应保留到下次启动:", err)
	}
	var parsed map[string]any
	if err := json.Unmarshal(marker, &parsed); err != nil || parsed["purpose"] != "restore" || parsed["phase"] != "restored" {
		t.Fatalf("标记阶段不符：%v %v", parsed, err)
	}
	prepared, err := migration.PrepareDatabase(context.Background(), path)
	if err != nil {
		t.Fatalf("恢复后的库未通过启动校验：%v", err)
	}
	prepared.Close()
	if _, err := os.Stat(filepath.Join(filepath.Dir(path), "backups", ".restore.lock")); !os.IsNotExist(err) {
		t.Fatalf("启动校验后保护标记应被清除: %v", err)
	}
}

func TestRestoreRejectsCorruptArchive(t *testing.T) {
	s, db, _ := restoreFixture(t)
	created, err := s.Create(context.Background(), "")
	if err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(created.Path)
	if err != nil {
		t.Fatal(err)
	}
	for i := 40; i < len(raw); i++ {
		raw[i] ^= 0xFF
	}
	if err := os.WriteFile(created.Path, raw, 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Restore(context.Background(), created.Name); err == nil {
		t.Fatal("损坏归档必须被拒绝")
	} else if !strings.Contains(err.Error(), "哈希不符") && !strings.Contains(err.Error(), "校验失败") && !strings.Contains(err.Error(), "解压") {
		t.Fatalf("拒绝原因不合预期：%v", err)
	}
	// 段 1 失败不触碰原库：探针不残留，原库连接仍然可用
	if _, err := db.Exec("SELECT COUNT(*) FROM games"); err != nil {
		t.Fatalf("原库应保持可用：%v", err)
	}
	entries, _ := os.ReadDir(s.dir)
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), ".restore-") || strings.HasSuffix(entry.Name(), ".tmp") {
			t.Fatalf("段 1 失败遗留探针文件：%s", entry.Name())
		}
	}
}

func TestRestoreRejectsOlderTableSet(t *testing.T) {
	s, db, _ := restoreFixture(t)
	if err := os.MkdirAll(s.dir, 0o755); err != nil {
		t.Fatal(err)
	}
	older := filepath.Join(s.dir, "holdexar-20000101-000000.db")
	plain, err := os.Create(older)
	if err != nil {
		t.Fatal(err)
	}
	plain.Close()
	if err := os.Remove(older); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("ATTACH ? AS legacy", older); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`CREATE TABLE legacy.games(appid INTEGER PRIMARY KEY); INSERT INTO legacy.games VALUES(1)`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("DETACH legacy"); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Restore(context.Background(), filepath.Base(older)); err == nil {
		t.Fatal("缺表备份必须被拒绝")
	} else if !strings.Contains(err.Error(), "缺表") {
		t.Fatalf("拒绝原因不合预期：%v", err)
	}
	if _, err := db.Exec("SELECT COUNT(*) FROM games"); err != nil {
		t.Fatalf("原库应保持可用：%v", err)
	}
}

func TestRestoreRejectsNewerSchemaVersion(t *testing.T) {
	s, db, _ := restoreFixture(t)
	defer func() {
		if !dbClosed(db) {
			db.Close()
		}
	}()
	future := filepath.Join(s.dir, "holdexar-20000101-010000.db")
	if err := os.MkdirAll(s.dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := copyDatabase(path0(t, s), future, 999999); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Restore(context.Background(), filepath.Base(future)); err == nil ||
		!strings.Contains(err.Error(), "拒绝恢复") {
		t.Fatalf("超前版本必须被拒绝：%v", err)
	}
}

// copyDatabase 复制库文件并改写 user_version（构造「未来版本」夹具）。
func copyDatabase(src, dest string, version int) error {
	raw, err := os.ReadFile(src)
	if err != nil {
		return err
	}
	if err := os.WriteFile(dest, raw, 0o644); err != nil {
		return err
	}
	db, err := sql.Open("sqlite", dest)
	if err != nil {
		return err
	}
	defer db.Close()
	_, err = db.Exec("PRAGMA wal_checkpoint(TRUNCATE)")
	if err != nil {
		return err
	}
	_, err = db.Exec(fmt.Sprintf("PRAGMA user_version=%d", version))
	return err
}

func path0(t *testing.T, s *Service) string {
	t.Helper()
	return s.dbPath
}

func dbClosed(db *sql.DB) bool {
	return db == nil
}

func TestRestoreFailureBeforeReplaceKeepsOriginalReadable(t *testing.T) {
	s, db, path := restoreFixture(t)
	// 保护标记被占：恢复在取保护前即失败，原库不受影响
	blocker, err := store.BeginBackupMaintenance(path)
	if err != nil {
		t.Fatal(err)
	}
	defer blocker()
	if _, err := s.Restore(context.Background(), "holdexar-19700101-000000.db.gz"); err == nil {
		t.Fatal("保护被占必须拒绝")
	}
	if _, err := db.Exec("SELECT COUNT(*) FROM games"); err != nil {
		t.Fatalf("原库应保持可用：%v", err)
	}
	_ = errors.Is
}
