//go:build windows

// dataprotocol2.go 数据回退、验收提交与保护释放（协议后半）。
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"golang.org/x/sys/windows"
)

func attemptHash(attempt string) string {
	sum := sha256.Sum256([]byte(attempt))
	return hex.EncodeToString(sum[:])[:24]
}

// crossProcessWriteLockPath 临时目录按库路径哈希命名的独占锁文件。
func crossProcessWriteLockPath(db string) string {
	sum := sha256.Sum256([]byte(strings.ToLower(filepath.Clean(absPath(db)))))
	return filepath.Join(os.TempDir(), fmt.Sprintf("holdexar-write-%x.lock", sum[:12]))
}

// acquireCrossProcessWriteLock OS 级字节段独占锁：持有进程死亡即自动释放。
func acquireCrossProcessWriteLock(db string) (func(), error) {
	path := crossProcessWriteLockPath(db)
	file, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		return nil, err
	}
	deadline := time.Now().Add(writeWaitTimeout)
	for {
		var overlapped windows.Overlapped
		// LOCKFILE_EXCLUSIVE_LOCK(2) | LOCKFILE_FAIL_IMMEDIATELY(1)
		if err := windows.LockFileEx(windows.Handle(file.Fd()), 3, 0, 1, 0, &overlapped); err == nil {
			break
		}
		if time.Now().After(deadline) {
			file.Close()
			return nil, errors.New("数据库写入锁等待超时")
		}
		time.Sleep(50 * time.Millisecond)
	}
	released := false
	return func() {
		if !released {
			released = true
			var overlapped windows.Overlapped
			_ = windows.UnlockFileEx(windows.Handle(file.Fd()), 0, 1, 0, &overlapped)
			_ = file.Close()
		}
	}, nil
}

// assertFilesIdle 数据库三件套独占打开尝试（有占用即拒）。
func assertFilesIdle(db string) error {
	for _, suffix := range []string{"", "-wal", "-shm"} {
		path := db + suffix
		if !fileExists(path) {
			continue
		}
		file, err := os.OpenFile(path, os.O_RDWR, 0)
		if err != nil {
			return errors.New("数据库仍被其他进程占用，已保留现场并停止恢复")
		}
		file.Close()
	}
	return nil
}

func requireChildrenStopped(tx map[string]any, childrenStopped bool) error {
	if !childrenStopped || txBool(tx, "launchPending") {
		return errors.New("尚未确认升级验收进程全部退出，拒绝恢复数据")
	}
	for _, child := range txChildren(tx) {
		entry, _ := child.(map[string]any)
		pid, _ := entry["pid"].(float64)
		if pid > 0 && pidAlive(int(pid)) {
			return errors.New("升级验收进程尚未退出或身份无法确认")
		}
	}
	return nil
}

// rollbackData 四阶段数据回退；失败数据保留为 update-<h>.failed.db。
func rollbackData(root string, childrenStopped bool) error {
	tx, err := transactionOrError(root)
	if err != nil {
		return err
	}
	if txBool(tx, "dataAccepted") {
		return errors.New("升级数据已接受，禁止自动还原旧数据")
	}
	if txBool(tx, "dataRollbackReleased") {
		return releaseCompletedRollback(root, tx)
	}
	if !txBool(tx, "dataPrepared") {
		return errors.New("升级原始快照尚未准备完成，拒绝覆盖数据")
	}
	if err := requireChildrenStopped(tx, childrenStopped); err != nil {
		return err
	}
	db := txString(tx, "dataDbPath")
	if err := matchingMarker(root, tx); err != nil {
		return err
	}
	releaseWriteLock, err := acquireCrossProcessWriteLock(db)
	if err != nil {
		return err
	}
	defer releaseWriteLock()
	if tx, err = transactionOrError(root); err != nil {
		return err
	}
	if err := matchingMarker(root, tx); err != nil {
		return err
	}
	if txBool(tx, "dataAccepted") {
		return errors.New("升级数据已接受，禁止自动还原旧数据")
	}
	if err := assertFilesIdle(db); err != nil {
		return err
	}
	phase := txString(tx, "dataRecoveryPhase")
	snapshot := txString(tx, "dataSnapshot")
	if snapshot != "" {
		if digest, err := fileSHA256Sum(snapshot); err != nil || digest != txString(tx, "dataSnapshotSha256") {
			return errors.New("升级原始快照已损坏或不完整，拒绝覆盖当前数据")
		}
	}
	failedDB := strings.Replace(snapshot, ".original.db", ".failed.db", 1)
	switch phase {
	case "prepared":
		var present []string
		for _, suffix := range []string{"", "-wal", "-shm"} {
			if fileExists(db + suffix) {
				present = append(present, suffix)
			}
		}
		if err := recordTx(root, txString(tx, "stage"), map[string]any{
			"dataRecoveryPhase": "preserving", "dataFailedDb": failedDB, "dataRecoveryFiles": present,
		}); err != nil {
			return err
		}
		phase = "preserving"
		fallthrough
	case "preserving":
		if err := assertFilesIdle(db); err != nil {
			return err
		}
		for _, suffix := range []string{"", "-wal", "-shm"} {
			source := db + suffix
			target := failedDB + suffix
			sourceExists := fileExists(source)
			targetExists := fileExists(target)
			if sourceExists && targetExists {
				return errors.New("恢复现场出现冲突文件，拒绝覆盖")
			}
			if sourceExists {
				if err := os.Rename(source, target); err != nil {
					return err
				}
			} else if !targetExists {
				return errors.New("恢复现场文件缺失，拒绝继续覆盖")
			}
		}
		if err := recordTx(root, txString(tx, "stage"), map[string]any{"dataRecoveryPhase": "restoring"}); err != nil {
			return err
		}
		phase = "restoring"
		fallthrough
	case "restoring":
		for _, suffix := range []string{"-wal", "-shm"} {
			if fileExists(db + suffix) {
				return errors.New("恢复目标出现未知数据库侧车文件，已停止覆盖")
			}
		}
		if fileExists(db) {
			if digest, err := fileSHA256Sum(db); err != nil || digest != txString(tx, "dataSnapshotSha256") {
				return errors.New("恢复目标出现未知数据，拒绝覆盖")
			}
		} else if snapshot != "" {
			staging := snapshot + ".restore-copy"
			if err := copyFile(snapshot, staging); err != nil {
				return err
			}
			if digest, err := fileSHA256Sum(staging); err != nil || digest != txString(tx, "dataSnapshotSha256") {
				os.Remove(staging)
				return errors.New("数据库恢复副本校验失败")
			}
			if file, err := os.OpenFile(staging, os.O_RDWR, 0); err == nil {
				file.Sync()
				file.Close()
			}
			if err := os.Rename(staging, db); err != nil {
				os.Remove(staging)
				return err
			}
		}
		if snapshot != "" {
			if result, err := quickCheck(db); err != nil || strings.ToLower(result) != "ok" {
				return fmt.Errorf("恢复后的数据库校验失败: %s, %v", result, err)
			}
		}
		if err := recordTx(root, txString(tx, "stage"), map[string]any{"dataRecoveryPhase": "restored"}); err != nil {
			return err
		}
	case "restored":
		if snapshot != "" {
			if digest, err := fileSHA256Sum(db); err != nil || digest != txString(tx, "dataSnapshotSha256") {
				return errors.New("已恢复数据库出现未知变更，已保留保护")
			}
		}
	default:
		return errors.New("数据库恢复阶段无法识别")
	}
	return nil
}

func releaseCompletedRollback(root string, tx map[string]any) error {
	db := txString(tx, "dataDbPath")
	marker, err := readMarker(filepath.Dir(db))
	if err != nil {
		return err
	}
	if marker == nil {
		return nil
	}
	if err := matchingMarker(root, tx); err != nil {
		return err
	}
	return releaseRestoreLock(filepath.Dir(db), txString(tx, "dataToken"))
}

// finishRollback 终态确认后释放保护（dataRollbackReleased）。
func finishRollback(root string) error {
	tx, err := transactionOrError(root)
	if err != nil {
		return err
	}
	if txBool(tx, "dataAccepted") {
		return errors.New("升级数据已接受，禁止释放回退保护")
	}
	if txBool(tx, "dataRollbackReleased") {
		return releaseCompletedRollback(root, tx)
	}
	if txString(tx, "dataRecoveryPhase") != "restored" || txString(tx, "stage") != stageRolledBack ||
		txString(tx, "sourceVersion") == "" {
		return errors.New("原程序指向与数据库回退尚未全部完成，保留数据保护")
	}
	state, _ := readCurrentState(root)
	if stateString(state, "version") != txString(tx, "sourceVersion") {
		return errors.New("原程序指向与数据库回退尚未全部完成，保留数据保护")
	}
	if err := requireChildrenStopped(tx, true); err != nil {
		return err
	}
	if err := matchingMarker(root, tx); err != nil {
		return err
	}
	db := txString(tx, "dataDbPath")
	releaseWriteLock, err := acquireCrossProcessWriteLock(db)
	if err != nil {
		return err
	}
	defer releaseWriteLock()
	if err := assertFilesIdle(db); err != nil {
		return err
	}
	for _, suffix := range []string{"-wal", "-shm"} {
		if fileExists(db + suffix) {
			return errors.New("已恢复数据库出现未知侧车文件，保留数据保护")
		}
	}
	if snapshot := txString(tx, "dataSnapshot"); snapshot != "" {
		if digest, err := fileSHA256Sum(db); err != nil || digest != txString(tx, "dataSnapshotSha256") {
			return errors.New("已恢复数据库出现未知变更，保留数据保护")
		}
	} else if fileExists(db) {
		return errors.New("已恢复数据库出现未知变更，保留数据保护")
	}
	if err := recordTx(root, txString(tx, "stage"), map[string]any{"dataRollbackReleased": true}); err != nil {
		return err
	}
	return releaseRestoreLock(filepath.Dir(db), txString(tx, "dataToken"))
}

// abortPreparation 准备期中止（尚未 prepare 成功时安全释放）。
func abortPreparation(root string, dataDir string) error {
	tx, err := transactionOrError(root)
	if err != nil {
		return err
	}
	if dataDir == "" {
		dataDir = txString(tx, "dataDir")
		if dataDir == "" {
			dataDir = filepath.Join(root, dataDirName)
		}
	}
	if txBool(tx, "dataPrepared") || txBool(tx, "dataAccepted") || txBool(tx, "launchPending") ||
		len(txChildren(tx)) > 0 || txString(tx, "sourceVersion") == "" ||
		currentVersionOf(root) != txString(tx, "sourceVersion") ||
		currentVersionOf(root) == txString(tx, "targetVersion") ||
		(txString(tx, "dataDir") != "" && !strings.EqualFold(absPath(txString(tx, "dataDir")), absPath(dataDir))) {
		return errors.New("升级准备状态无法确认安全，保留数据保护")
	}
	marker, err := readMarker(dataDir)
	if err != nil {
		return err
	}
	if marker == nil {
		return nil
	}
	if markerString(marker, "purpose") != updatePurpose || markerString(marker, "token") == "" ||
		markerString(marker, "attemptId") != txString(tx, "attemptId") ||
		markerString(marker, "installRoot") != absPath(root) {
		return errors.New("升级准备保护与当前事务不匹配，拒绝解除")
	}
	return releaseRestoreLock(dataDir, markerString(marker, "token"))
}

// acceptData 验收提交：dataAccepted + 结构版本相容记录 + 释放保护。
func acceptData(root string) error {
	tx, err := transactionOrError(root)
	if err != nil {
		return err
	}
	if !txBool(tx, "dataPrepared") {
		return errors.New("当前升级数据不可接受")
	}
	db := txString(tx, "dataDbPath")
	marker, err := readMarker(filepath.Dir(db))
	if err != nil {
		return err
	}
	if marker != nil && markerString(marker, "purpose") != updatePurpose {
		return errors.New("数据库维护保护尚未解除")
	}
	if marker != nil || !txBool(tx, "dataAccepted") {
		if err := matchingMarker(root, tx); err != nil {
			return err
		}
	}
	releaseWriteLock, err := acquireCrossProcessWriteLock(db)
	if err != nil {
		return err
	}
	defer releaseWriteLock()
	current, err := transactionOrError(root)
	if err != nil {
		return err
	}
	if txString(current, "attemptId") != txString(tx, "attemptId") {
		return errors.New("升级验收事务已变化，保留数据保护")
	}
	accepted := txBool(current, "dataAccepted")
	schema := 0
	if accepted {
		value, ok := current["dataSchemaVersion"].(float64)
		if !ok {
			return errors.New("缺少已接受数据的结构版本凭据，拒绝猜测相容程序")
		}
		schema = int(value)
	} else {
		if schema, err = schemaVersionOf(db); err != nil {
			return err
		}
	}
	target := txString(current, "targetVersion")
	if !isManagedVersionName(target) {
		return errors.New("已验收程序版本无效，保留数据保护")
	}
	compatibility, err := readDataCompatibility(root)
	if err != nil {
		return err
	}
	sameDatabase := compatibility != nil && strings.EqualFold(
		absPath(fmt.Sprintf("%v", compatibility["dataDir"])), absPath(filepath.Dir(db)))
	versions := []string{}
	if sameDatabase {
		if list, ok := compatibility["versions"].([]any); ok {
			for _, item := range list {
				if text, ok := item.(string); ok {
					versions = append(versions, text)
				}
			}
		}
	}
	known := false
	for _, version := range versions {
		if version == target {
			known = true
		}
	}
	if !known {
		versions = append(versions, target)
	}
	fields := map[string]any{"dataAccepted": true, "dataSchemaVersion": schema, "dataRecoveryPhase": "accepted"}
	if !accepted || marker != nil {
		if err := recordTx(root, txString(current, "stage"), fields); err != nil {
			return err
		}
	}
	if err := writeDataCompatibility(root, map[string]any{
		"dataDir": filepath.Dir(db), "schemaVersion": schema, "versions": versions,
	}); err != nil {
		return err
	}
	if marker != nil {
		return releaseRestoreLock(filepath.Dir(db), txString(tx, "dataToken"))
	}
	return nil
}

func currentVersionOf(root string) string {
	state, _ := readCurrentState(root)
	return stateString(state, "version")
}

func txChildren(tx map[string]any) []any {
	children, _ := tx["children"].([]any)
	return children
}
