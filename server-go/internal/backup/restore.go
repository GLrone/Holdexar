package backup

import (
	"compress/gzip"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"holdexar/server-go/internal/occupancy"
	"holdexar/server-go/internal/store"
)

type RestoreResult struct {
	Restored   bool   `json:"restored"`
	Name       string `json:"name"`
	RolledBack bool   `json:"rolledBack"`
	Restarting bool   `json:"restarting,omitempty"`
}

const (
	replaceAttempts     = 6
	replaceRetrySeconds = 300 * time.Millisecond
)

// ErrRestartPending 数据库句柄已关闭后的失败，须重启进程重载。
var ErrRestartPending = errors.New("恢复中断，服务将重启后继续")

// Restore 验档后停写并固化回滚快照与保护标记再替换；替换成功后失败不回滚，交由下一进程校验处置。
func (s *Service) Restore(ctx context.Context, name string) (RestoreResult, error) {
	result := RestoreResult{Name: name}
	if !backupName(name) {
		return result, ErrName
	}
	if err := store.CheckMaintenance(s.dbPath); err != nil {
		return result, err
	}
	src := s.dbPath
	probe, err := s.stageVerifiedArchive(ctx, name, src)
	if err != nil {
		return result, err
	}
	if err := occupancy.BeginMaintenance(occupancy.MaintenanceStorage); err != nil {
		os.Remove(probe)
		return result, fmt.Errorf("有写入正在进行，请稍后重试：%w", err)
	}
	ended := false
	endOccupancy := func() {
		if !ended {
			ended = true
			occupancy.EndMaintenance()
		}
	}
	if err := s.pauseWrites(ctx); err != nil {
		os.Remove(probe)
		endOccupancy()
		return result, fmt.Errorf("有写入正在进行，请稍后重试：%w", err)
	}
	paused := true
	resume := func() {
		if paused {
			paused = false
			s.gate.ResumeWrites()
		}
	}
	token, release, err := store.BeginRestoreMaintenance(src)
	if err != nil {
		os.Remove(probe)
		resume()
		endOccupancy()
		return result, err
	}
	released := false
	releaseOnce := func() {
		if !released {
			released = true
			_ = release()
		}
	}
	rollback := src + ".rollback.db"
	staging := src + ".restoring.db"
	replaced := false
	defer func() {
		os.Remove(staging)
		if !replaced {
			os.Remove(rollback)
			releaseOnce()
		}
		resume()
		endOccupancy()
	}()

	os.Remove(rollback)
	if err := store.Snapshot(ctx, s.db, rollback); err != nil {
		os.Remove(probe)
		return result, fmt.Errorf("原库快照未能生成，已保留原库：%w", err)
	}
	digest, err := fileSHA256(rollback)
	if err != nil {
		os.Remove(probe)
		return result, err
	}
	if err := store.ProtectRestoreRecovery(src, token, rollback, digest); err != nil {
		os.Remove(probe)
		return result, err
	}

	// 释放本进程数据库句柄后才能替换文件；此后任何失败都须重启重载
	if err := s.db.Close(); err != nil {
		return result, errors.Join(fmt.Errorf("数据库连接未能释放，原库文件完好：%w", err), ErrRestartPending)
	}
	if err := replaceFile(probe, src); err != nil {
		return result, errors.Join(fmt.Errorf("主库文件替换未完成，原库已保留：%w", err), ErrRestartPending)
	}
	replaced = true
	// 陈旧 WAL 清理：残留旧帧会被新库重放导致损坏
	os.Remove(src + "-wal")
	os.Remove(src + "-shm")
	if err := store.MarkRestoreReplaced(src, token); err != nil {
		// 阶段改写失败仍保持 replacing 标记供下轮启动拒启，且句柄已释放必须重启。
		return RestoreResult{Restored: true, Name: name, Restarting: true}, nil
	}
	return RestoreResult{Restored: true, Name: name, Restarting: true}, nil
}

// pauseWrites 在 10s 内按 300ms 重试进入维护窗口，超时或上下文取消时返回。
func (s *Service) pauseWrites(ctx context.Context) error {
	deadline := time.Now().Add(10 * time.Second)
	for {
		err := s.gate.PauseWrites(ctx)
		if err == nil || !errors.Is(err, store.ErrWritesPaused) {
			return err
		}
		if time.Now().After(deadline) {
			return err
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(300 * time.Millisecond):
		}
	}
}

func (s *Service) stageVerifiedArchive(ctx context.Context, name, src string) (string, error) {
	file, err := s.open(name)
	if err != nil {
		return "", err
	}
	defer file.Close()
	compressed := strings.HasSuffix(name, ".gz")
	if compressed {
		var stored metadata
		if meta, err := os.OpenRoot(s.dir); err == nil {
			if m, err := meta.Open(name + metadataSuffix); err == nil {
				_ = json.NewDecoder(io.LimitReader(m, 8192)).Decode(&stored)
				m.Close()
			}
			meta.Close()
		}
		if stored.SHA256 != "" {
			hash := sha256.New()
			if _, err := io.Copy(hash, contextReader{ctx, file}); err != nil {
				return "", err
			}
			if !strings.EqualFold(stored.SHA256, hex.EncodeToString(hash.Sum(nil))) {
				return "", fmt.Errorf("备份哈希不符（归档不完整或已损坏）: %s", name)
			}
			if _, err := file.Seek(0, io.SeekStart); err != nil {
				return "", err
			}
		}
	}
	probe, err := os.CreateTemp(s.dir, ".restore-*.tmp")
	if err != nil {
		return "", err
	}
	probeName := probe.Name()
	removeProbe := func() { os.Remove(probeName) }
	if compressed {
		reader, err := gzip.NewReader(file)
		if err != nil {
			probe.Close()
			removeProbe()
			return "", fmt.Errorf("备份归档无法解压: %s", name)
		}
		if _, err := io.Copy(probe, contextReader{ctx, reader}); err != nil {
			probe.Close()
			removeProbe()
			reader.Close()
			return "", fmt.Errorf("备份归档解压未完成（可能已截断）: %s", name)
		}
		reader.Close()
	} else {
		if _, err := file.Seek(0, io.SeekStart); err != nil {
			probe.Close()
			removeProbe()
			return "", err
		}
		if _, err := io.Copy(probe, contextReader{ctx, file}); err != nil {
			probe.Close()
			removeProbe()
			return "", err
		}
	}
	if err := probe.Close(); err != nil {
		removeProbe()
		return "", err
	}
	ok, _, err := inspectDatabase(ctx, probeName, true)
	if ctx.Err() != nil {
		removeProbe()
		return "", ctx.Err()
	}
	if err != nil || !ok {
		removeProbe()
		return "", fmt.Errorf("备份文件校验失败: %s", name)
	}
	backupTables, err := tableNames(probeName)
	if err != nil {
		removeProbe()
		return "", err
	}
	currentTables, err := tableNames(src)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		removeProbe()
		return "", err
	}
	if len(currentTables) > 0 && !superset(backupTables, currentTables) {
		removeProbe()
		missing := missingKeys(backupTables, currentTables)
		return "", fmt.Errorf("备份缺表（版本过旧，恢复会丢数据）: %s", strings.Join(missing, ", "))
	}
	version, err := schemaVersion(probeName)
	if err != nil {
		removeProbe()
		return "", err
	}
	if version > store.CurrentSchemaVersion {
		removeProbe()
		return "", fmt.Errorf("备份数据库结构版本 v%d 高于当前程序支持的 v%d，拒绝恢复", version, store.CurrentSchemaVersion)
	}
	return probeName, nil
}

func replaceFile(from, src string) error {
	var last error
	for attempt := 0; attempt < replaceAttempts; attempt++ {
		if err := os.Rename(from, src); err == nil {
			return nil
		} else {
			last = err
		}
		time.Sleep(replaceRetrySeconds)
	}
	return fmt.Errorf("主库文件被占用，替换未完成（已重试 %d 次）：%w", replaceAttempts, last)
}

func tableNames(path string) ([]string, error) {
	if _, err := os.Stat(path); errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	db, err := sql.Open("sqlite", "file:"+filepath.ToSlash(path)+"?mode=ro")
	if err != nil {
		return nil, err
	}
	defer db.Close()
	rows, err := db.Query("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var names []string
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			return nil, err
		}
		names = append(names, name)
	}
	return names, rows.Err()
}

func schemaVersion(path string) (int, error) {
	db, err := sql.Open("sqlite", "file:"+filepath.ToSlash(path)+"?mode=ro")
	if err != nil {
		return 0, err
	}
	defer db.Close()
	var version int
	if err := db.QueryRow("PRAGMA user_version").Scan(&version); err != nil {
		return 0, err
	}
	return version, nil
}

func superset(super, sub []string) bool {
	present := make(map[string]bool, len(super))
	for _, name := range super {
		present[name] = true
	}
	for _, name := range sub {
		if !present[name] {
			return false
		}
	}
	return true
}

func missingKeys(super, sub []string) []string {
	present := make(map[string]bool, len(super))
	for _, name := range super {
		present[name] = true
	}
	var missing []string
	for _, name := range sub {
		if !present[name] {
			missing = append(missing, name)
		}
	}
	return missing
}

func fileSHA256(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	hash := sha256.New()
	if _, err := io.Copy(hash, file); err != nil {
		return "", err
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}
