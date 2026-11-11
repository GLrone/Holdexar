//go:build windows

// dataprotocol.go 升级验收期的数据保护协议：prepare（原始快照+update 保护标记）、
// rollback（四阶段回退，失败数据留档）、finish_rollback（释放保护）、
// abort_preparation（准备期中止）、accept（验收提交+相容记录）。
// 标记/事务语义对齐 app/core/update_data.py 与 db_safety.py。
package main

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

const (
	writeWaitTimeout = 60 * time.Second
	updatePurpose    = "update"
)

var errNoTransaction = errors.New("升级事务不存在或无法读取")

func dbPathOf(dataDir string) string {
	return filepath.Join(dataDir, appSlug+".db")
}

func markerPath(dataDir string) string {
	return filepath.Join(dataDir, "backups", ".restore.lock")
}

func readMarker(dataDir string) (map[string]any, error) {
	raw, err := os.ReadFile(markerPath(dataDir))
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, fmt.Errorf("升级数据保护记录无法读取，已停止自动处理: %w", err)
	}
	var marker map[string]any
	if err := json.Unmarshal(raw, &marker); err != nil || marker == nil {
		return nil, errors.New("升级数据保护记录无效")
	}
	return marker, nil
}

func markerString(marker map[string]any, key string) string {
	if marker == nil {
		return ""
	}
	value, _ := marker[key].(string)
	return value
}

// acquireRestoreLock 建保护标记（O_EXCL）；purpose=update/restore 不参与陈旧清理。
func acquireRestoreLock(dataDir, purpose, installRoot, attemptID string) (string, error) {
	path := markerPath(dataDir)
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return "", err
	}
	token := fmt.Sprintf("%d-%d-%s", os.Getpid(), time.Now().UnixNano(), randomHex(6))
	marker := map[string]any{
		"pid": os.Getpid(), "created_at": time.Now().Format(time.RFC3339Nano),
		"token": token, "purpose": purpose,
		"installRoot": installRoot, "attemptId": attemptID,
		"dbPath": dbPathOf(dataDir),
	}
	payload, _ := json.Marshal(marker)
	file, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return "", errors.New("数据库维护正在进行中")
	}
	if _, err = file.Write(payload); err != nil {
		file.Close()
		os.Remove(path)
		return "", err
	}
	if err = file.Sync(); err != nil {
		file.Close()
		os.Remove(path)
		return "", err
	}
	return token, file.Close()
}

// releaseRestoreLock 仅 token 归属人可删（保护记录可能已被原子改写）。
func releaseRestoreLock(dataDir, token string) error {
	path := markerPath(dataDir)
	raw, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	var marker map[string]any
	if json.Unmarshal(raw, &marker) != nil || markerString(marker, "token") != token {
		return nil
	}
	return os.Remove(path)
}

// updateRestoreMarker 原子改写保护记录（保留 token 所有权校验）。
func updateRestoreMarker(dataDir, token string, mutate func(map[string]any)) error {
	marker, err := readMarker(dataDir)
	if err != nil {
		return err
	}
	if markerString(marker, "token") != token {
		return errors.New("升级数据保护与当前事务不匹配，已停止自动处理")
	}
	mutate(marker)
	path := markerPath(dataDir)
	payload, _ := json.Marshal(marker)
	staging := path + ".recovery.tmp"
	if err := os.WriteFile(staging, payload, 0o600); err != nil {
		return err
	}
	if err := os.Rename(staging, path); err != nil {
		os.Remove(staging)
		return err
	}
	return nil
}

// matchingMarker 校验保护记录与本事务逐字段匹配。
func matchingMarker(root string, tx map[string]any) error {
	dataDir := txString(tx, "dataDir")
	marker, err := readMarker(dataDir)
	if err != nil {
		return err
	}
	if marker == nil || markerString(marker, "purpose") != updatePurpose ||
		markerString(marker, "token") != txString(tx, "dataToken") || txString(tx, "dataToken") == "" ||
		markerString(marker, "attemptId") != txString(tx, "attemptId") ||
		markerString(marker, "installRoot") != absPath(root) ||
		dbPathOf(dataDir) != txString(tx, "dataDbPath") {
		return errors.New("升级数据保护与当前事务不匹配，已停止自动处理")
	}
	return nil
}

func transactionOrError(root string) (map[string]any, error) {
	tx := readTransaction(root)
	if tx == nil || txString(tx, "attemptId") == "" {
		return nil, errNoTransaction
	}
	return tx, nil
}

// recordTx 更新事务字段并 fsync 事务文件（更新协议的关键持久化点）。
func recordTx(root string, stage string, fields map[string]any) error {
	if err := updateTransaction(root, stage, fields); err != nil {
		return err
	}
	path := filepath.Join(root, stateDirName, transactionF)
	if file, err := os.OpenFile(path, os.O_RDWR, 0); err == nil {
		file.Sync()
		file.Close()
	}
	return nil
}

func sqliteURI(path string) string {
	p := filepath.ToSlash(absPath(path))
	return (&url.URL{Scheme: "file", Path: "/" + strings.TrimPrefix(p, "/"), RawQuery: "mode=ro"}).String()
}

func snapshotTo(src, dest string) error {
	connection, err := sql.Open("sqlite", sqliteURI(src))
	if err != nil {
		return err
	}
	defer connection.Close()
	if _, err := connection.Exec("PRAGMA busy_timeout=30000"); err != nil {
		return err
	}
	if _, err := connection.Exec("VACUUM INTO ?", dest); err != nil {
		return err
	}
	return nil
}

func quickCheck(path string) (string, error) {
	connection, err := sql.Open("sqlite", sqliteURI(path))
	if err != nil {
		return "", err
	}
	defer connection.Close()
	var result string
	err = connection.QueryRow("PRAGMA quick_check").Scan(&result)
	return result, err
}

func schemaVersionOf(path string) (int, error) {
	connection, err := sql.Open("sqlite", sqliteURI(path))
	if err != nil {
		return 0, err
	}
	defer connection.Close()
	var version int
	err = connection.QueryRow("PRAGMA user_version").Scan(&version)
	return version, err
}

func fileSHA256Sum(path string) (string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}

// prepare 升级原始快照与保护标记（dataPrepared）。
func prepareData(root, dataDir, attempt string) error {
	tx, err := transactionOrError(root)
	if err != nil {
		return err
	}
	if txString(tx, "attemptId") != attempt || txBool(tx, "dataPrepared") || txBool(tx, "dataAccepted") {
		return errors.New("升级数据准备与当前事务不匹配")
	}
	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		return err
	}
	db := dbPathOf(dataDir)
	name := attemptHash(attempt)
	snapshot := filepath.Join(dataDir, "backups", fmt.Sprintf("update-%s.original.db", name))
	if fileExists(snapshot) {
		return errors.New("本次升级的原始快照已存在，拒绝覆盖")
	}
	releaseWriteLock, err := acquireCrossProcessWriteLock(db)
	if err != nil {
		return err
	}
	defer releaseWriteLock()
	token, err := acquireRestoreLock(dataDir, updatePurpose, root, attempt)
	if err != nil {
		return err
	}
	snapshotReady := false
	defer func() {
		if !snapshotReady {
			_ = releaseRestoreLock(dataDir, token)
		}
	}()
	hadDatabase := fileExists(db)
	digest := ""
	if hadDatabase {
		if err := snapshotTo(db, snapshot); err != nil {
			return fmt.Errorf("升级原始数据库快照失败：%w", err)
		}
		if result, err := quickCheck(snapshot); err != nil || strings.ToLower(result) != "ok" {
			os.Remove(snapshot)
			return fmt.Errorf("升级原始数据库快照校验失败: %s, %v", result, err)
		}
		if digest, err = fileSHA256Sum(snapshot); err != nil {
			return err
		}
		if file, err := os.OpenFile(snapshot, os.O_RDWR, 0); err == nil {
			file.Sync()
			file.Close()
		}
	}
	snapshotReady = true
	snapshotField := any(nil)
	if hadDatabase {
		snapshotField = snapshot
	}
	digestField := any(nil)
	if hadDatabase {
		digestField = digest
	}
	return recordTx(root, txString(tx, "stage"), map[string]any{
		"dataDir": dataDir, "dataDbPath": db,
		"dataHadDatabase": hadDatabase,
		"dataSnapshot":    snapshotField, "dataSnapshotSha256": digestField,
		"dataToken": token, "dataPrepared": true, "dataAccepted": false,
		"dataRecoveryPhase": "prepared",
	})
}
