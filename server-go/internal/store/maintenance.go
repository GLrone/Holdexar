package store

import (
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

var ErrMaintenanceProtected = errors.New("数据库维护保护尚未解除")

type maintenanceMarker struct {
	PID            int    `json:"pid"`
	CreatedAt      string `json:"created_at"`
	Token          string `json:"token"`
	Purpose        string `json:"purpose"`
	DBPath         string `json:"dbPath"`
	Phase          string `json:"phase,omitempty"`
	RollbackPath   string `json:"rollbackPath,omitempty"`
	RollbackSHA256 string `json:"rollbackSha256,omitempty"`
}

const (
	RestorePhaseReplacing = "replacing"
	RestorePhaseRestored  = "restored"
)

func maintenancePath(dbPath string) string {
	return filepath.Join(filepath.Dir(dbPath), "backups", ".restore.lock")
}

func CheckMaintenance(dbPath string) error {
	path := maintenancePath(dbPath)
	before, err := os.Lstat(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	var marker struct {
		PID     int             `json:"pid"`
		Purpose json.RawMessage `json:"purpose"`
	}
	if !before.Mode().IsRegular() {
		return ErrMaintenanceProtected
	}
	raw, err := os.ReadFile(path)
	if err != nil || json.Unmarshal(raw, &marker) != nil || marker.PID <= 0 ||
		(len(marker.Purpose) > 0 && string(marker.Purpose) != `"maintenance"`) || !processProvenDead(marker.PID) {
		return ErrMaintenanceProtected
	}
	return removeOwnedMarker(path, before)
}

func HasRestoreMarker(dbPath string) bool {
	raw, err := os.ReadFile(maintenancePath(dbPath))
	if err != nil {
		return false
	}
	var marker struct {
		Purpose string `json:"purpose"`
	}
	return json.Unmarshal(raw, &marker) == nil && marker.Purpose == "restore"
}

func removeOwnedMarker(path string, before os.FileInfo) error {
	after, err := os.Lstat(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if !os.SameFile(before, after) || before.Size() != after.Size() || !before.ModTime().Equal(after.ModTime()) {
		return ErrMaintenanceProtected
	}
	return os.Remove(path)
}

func BeginBackupMaintenance(dbPath string) (func() error, error) {
	return beginMaintenance(dbPath, "maintenance")
}

// BeginRestoreMaintenance 建立不参与陈旧 PID 清理的恢复保护标记。
func BeginRestoreMaintenance(dbPath string) (string, func() error, error) {
	token := rand.Text()
	end, err := beginMaintenanceWithToken(dbPath, "restore", token)
	return token, end, err
}

func beginMaintenance(dbPath, purpose string) (func() error, error) {
	end, err := beginMaintenanceWithToken(dbPath, purpose, rand.Text())
	return end, err
}

func beginMaintenanceWithToken(dbPath, purpose, token string) (func() error, error) {
	path := maintenancePath(dbPath)
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return nil, err
	}
	if err := CheckMaintenance(dbPath); err != nil {
		return nil, err
	}
	marker := maintenanceMarker{PID: os.Getpid(), Token: token, Purpose: purpose, DBPath: dbPath,
		CreatedAt: time.Now().Format(time.RFC3339Nano)}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if errors.Is(err, os.ErrExist) {
		return nil, ErrMaintenanceProtected
	}
	if err != nil {
		return nil, err
	}
	writeErr := json.NewEncoder(file).Encode(marker)
	syncErr := file.Sync()
	before, statErr := file.Stat()
	if err := errors.Join(writeErr, syncErr, statErr, file.Close()); err != nil {
		if before != nil {
			err = errors.Join(err, removeOwnedMarker(path, before))
		}
		return nil, fmt.Errorf("无法建立备份保护：%w", err)
	}
	// 按 token 而非创建时 stat 判权：恢复期间标记会被原子重命名替换。
	return func() error { return removeMarkerByToken(path, token) }, nil
}

func removeMarkerByToken(path, token string) error {
	raw, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	var marker maintenanceMarker
	if err := json.Unmarshal(raw, &marker); err != nil || marker.Token != token {
		return nil
	}
	return os.Remove(path)
}

func updateRestoreMarker(dbPath, token string, mutate func(*maintenanceMarker)) error {
	path := maintenancePath(dbPath)
	raw, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	var marker maintenanceMarker
	if err := json.Unmarshal(raw, &marker); err != nil || marker.Token != token {
		return fmt.Errorf("数据库恢复保护的所有权已变更，拒绝替换数据库")
	}
	mutate(&marker)
	payload, err := json.Marshal(marker)
	if err != nil {
		return err
	}
	staging := path + ".recovery.tmp"
	if err := os.WriteFile(staging, payload, 0600); err != nil {
		return err
	}
	if err := os.Rename(staging, path); err != nil {
		os.Remove(staging)
		return err
	}
	if file, err := os.OpenFile(path, os.O_RDWR, 0); err == nil {
		file.Sync()
		file.Close()
	}
	return nil
}

func ProtectRestoreRecovery(dbPath, token, rollbackPath, digest string) error {
	return updateRestoreMarker(dbPath, token, func(m *maintenanceMarker) {
		m.Phase = RestorePhaseReplacing
		m.RollbackPath = rollbackPath
		m.RollbackSHA256 = digest
	})
}

func MarkRestoreReplaced(dbPath, token string) error {
	return updateRestoreMarker(dbPath, token, func(m *maintenanceMarker) {
		m.Phase = RestorePhaseRestored
	})
}

// CheckRestoreMarker 启动期检查恢复保护：仅 restored 阶段放行（待迁移校验后清除），其余阶段或损坏标记拒绝启动。
func CheckRestoreMarker(dbPath string) error {
	path := maintenancePath(dbPath)
	raw, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	var marker maintenanceMarker
	if err := json.Unmarshal(raw, &marker); err != nil {
		return fmt.Errorf("数据库维护保护记录无法读取，已停止启动；请保留数据目录和保护记录")
	}
	if marker.Purpose != "restore" {
		return nil
	}
	if marker.Phase == RestorePhaseRestored {
		return nil
	}
	rollback := "未找到原库副本，当前数据状态尚未确认。"
	if marker.RollbackPath != "" {
		if _, err := os.Stat(marker.RollbackPath); err == nil {
			rollback = "待校验的原库副本保留在 " + marker.RollbackPath + "。"
		}
	}
	return fmt.Errorf("上次原库恢复未完成，已停止写入；%s请保留整个数据目录并联系维护者恢复原库，勿删除维护保护文件", rollback)
}

func ClearRestoreMarker(dbPath string) error {
	path := maintenancePath(dbPath)
	raw, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	var marker maintenanceMarker
	if err := json.Unmarshal(raw, &marker); err != nil || marker.Purpose != "restore" {
		return nil
	}
	before, err := os.Lstat(path)
	if err != nil {
		return err
	}
	if err := removeOwnedMarker(path, before); err != nil {
		return err
	}
	if marker.RollbackPath != "" {
		os.Remove(marker.RollbackPath)
	}
	return nil
}

func IsUpdateMarker(dbPath string) bool {
	raw, err := os.ReadFile(maintenancePath(dbPath))
	if err != nil {
		return false
	}
	var marker struct {
		Purpose string `json:"purpose"`
	}
	return json.Unmarshal(raw, &marker) == nil && marker.Purpose == "update"
}
