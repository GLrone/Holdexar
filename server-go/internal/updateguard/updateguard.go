//go:build windows

// Package updateguard 升级验收期的数据保护：启动授权校验、写保护、页面就绪位。
// 语义对齐 app/core/update_data.py（validate_startup / update-ready / 写保护中间件）。
package updateguard

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"time"

	"holdexar/server-go/internal/config"
	"holdexar/server-go/internal/store"
)

const registrationWait = 3 * time.Second

var (
	ErrProtected      = errors.New("升级数据仍受保护，缺少本次启动授权")
	ErrNotDuringCheck = errors.New("当前不在更新验收期间")
)

type state struct {
	protected   atomic.Bool
	attemptID   atomic.Value // string
	token       atomic.Value // string
	pageReady   atomic.Bool
	dataDirPath atomic.Value // string
}

var current state

// Active 验收保护是否生效（健康载荷的 writeProtected 位）。
func Active() bool { return current.protected.Load() }

// AttemptID 当前验收事务 ID。
func AttemptID() string {
	value, _ := current.attemptID.Load().(string)
	return value
}

// PageReady 页面验收就绪位。
func PageReady() bool { return current.pageReady.Load() }

// DataDir 受保护数据目录。
func DataDir() string {
	value, _ := current.dataDirPath.Load().(string)
	return value
}

// MarkerPath data/backups/.restore.lock。
func MarkerPath(dataDir string) string {
	return filepath.Join(dataDir, "backups", ".restore.lock")
}

func readMarker(dataDir string) (map[string]any, error) {
	raw, err := os.ReadFile(MarkerPath(dataDir))
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	var marker map[string]any
	if json.Unmarshal(raw, &marker) != nil {
		return nil, fmt.Errorf("升级数据保护记录无法读取，已停止自动处理")
	}
	return marker, nil
}

func markerString(marker map[string]any, key string) string {
	value, _ := marker[key].(string)
	return value
}

func txChildrenRegistered(tx map[string]any, pid int) bool {
	children, _ := tx["children"].([]any)
	for _, child := range children {
		entry, _ := child.(map[string]any)
		pidValue, _ := entry["pid"].(float64)
		stopped, _ := entry["stopped"].(bool)
		if int(pidValue) == pid && !stopped {
			return true
		}
	}
	return false
}

func processAlive(pid int) bool {
	handle, err := os.FindProcess(pid)
	if err != nil {
		return false
	}
	_ = handle
	// Windows 无 0 信号探测：OpenProcess 判定
	return processAliveWin(pid)
}

// validateStartup 启动授权：无保护标记且无验收环境 → 正常启动；
// 有标记/环境 → 逐项校验（3 秒登记窗口）并返回数据令牌。
func validateStartup(dataDir string) (string, error) {
	marker, err := readMarker(dataDir)
	if err != nil {
		return "", err
	}
	if marker != nil && markerString(marker, "purpose") != "update" {
		if err := store.CheckMaintenance(dbPathOf(dataDir)); err != nil {
			return "", err
		}
		marker, _ = readMarker(dataDir)
	}
	attempt := os.Getenv(config.EnvPrefix + "UPDATE_ATTEMPT_ID")
	if marker == nil && attempt == "" {
		return "", nil
	}
	install := os.Getenv(config.EnvPrefix + "INSTALL_ROOT")
	token := os.Getenv(config.EnvPrefix + "UPDATE_DATA_TOKEN")
	if marker == nil || attempt == "" || install == "" || token == "" {
		return "", ErrProtected
	}
	root := filepath.Clean(install)
	txPath := filepath.Join(root, "state", "transaction.json")
	raw, err := os.ReadFile(txPath)
	if err != nil {
		return "", fmt.Errorf("升级事务不存在或无法读取: %w", err)
	}
	var tx map[string]any
	if json.Unmarshal(raw, &tx) != nil {
		return "", fmt.Errorf("升级事务无法读取")
	}
	deadline := time.Now().Add(registrationWait)
	for boolValue(tx["dataProbeRegistrationRequired"]) {
		if !processAlive(int(numberValue(marker["pid"]))) {
			return "", errors.New("升级宿主已退出，拒绝初始化受保护数据库")
		}
		if !boolValue(tx["launchPending"]) && txChildrenRegistered(tx, os.Getpid()) {
			break
		}
		if time.Now().After(deadline) {
			return "", errors.New("升级启动尚未完成进程登记，拒绝初始化数据库")
		}
		time.Sleep(20 * time.Millisecond)
		raw, err = os.ReadFile(txPath)
		if err != nil || json.Unmarshal(raw, &tx) != nil {
			return "", fmt.Errorf("升级事务无法读取")
		}
	}
	if err := matchingMarker(marker, tx, attempt, token, dataDir, root); err != nil {
		return "", err
	}
	if !boolValue(tx["dataPrepared"]) || boolValue(tx["dataAccepted"]) ||
		markerString(tx, "dataRecoveryPhase") != "prepared" {
		return "", errors.New("本次启动的升级数据授权无效")
	}
	return token, nil
}

func txString(tx map[string]any, key string) string {
	value, _ := tx[key].(string)
	return value
}

func matchingMarker(marker map[string]any, tx map[string]any, attempt, token, dataDir, root string) error {
	if markerString(marker, "purpose") != "update" || markerString(marker, "token") != token ||
		token == "" || markerString(marker, "attemptId") != attempt ||
		markerString(marker, "installRoot") != filepath.Clean(root) ||
		filepath.Join(filepath.Clean(dataDir), "holdexar.db") != markerString(tx, "dataDbPath") {
		return errors.New("升级数据保护与当前事务不匹配，已停止自动处理")
	}
	if txString(tx, "attemptId") != attempt || txString(tx, "dataToken") != token ||
		markerString(tx, "dataDir") != filepath.Clean(dataDir) {
		return errors.New("本次启动的升级数据授权无效")
	}
	return nil
}

func boolValue(value any) bool {
	result, _ := value.(bool)
	return result
}

func numberValue(value any) float64 {
	result, _ := value.(float64)
	return result
}

// Activate 启动期调用：校验通过后进入验收保护态（写保护+验收位）。
// 返回数据令牌（未处于保护态时为空串）。
func Activate(dataDir string) (string, error) {
	token, err := validateStartup(dataDir)
	if err != nil {
		return "", err
	}
	if token == "" {
		return "", nil
	}
	current.attemptID.Store(os.Getenv(config.EnvPrefix + "UPDATE_ATTEMPT_ID"))
	current.token.Store(token)
	current.dataDirPath.Store(filepath.Clean(dataDir))
	current.protected.Store(true)
	return token, nil
}

// SetPageReady 页面验收就绪（update-ready 端点调用）。
func SetPageReady(attemptID string) error {
	if !Active() || AttemptID() != attemptID {
		return ErrNotDuringCheck
	}
	current.pageReady.Store(true)
	return nil
}

// Token 写闸维护授权令牌（与恢复/收缩维护共用放行通道）。
func Token() string {
	value, _ := current.token.Load().(string)
	return value
}

func dbPathOf(dataDir string) string {
	return filepath.Join(dataDir, "holdexar.db")
}

// ClearProtection 验收提交后解除保护位。
func ClearProtection() {
	current.protected.Store(false)
	current.pageReady.Store(false)
}

// HandleUpdateReady POST /system/update-ready：页面验收就绪登记。
func HandleUpdateReady(w http.ResponseWriter, r *http.Request) {
	var body struct {
		AttemptID string `json:"attemptId"`
	}
	_ = json.NewDecoder(http.MaxBytesReader(w, r.Body, 8192)).Decode(&body)
	if err := SetPageReady(body.AttemptID); err != nil {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusConflict)
		_ = json.NewEncoder(w).Encode(map[string]string{"detail": "当前不在更新验收期间"})
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]bool{"ready": true})
}

// Middleware 验收期写保护：非 GET/HEAD/OPTIONS 的 /api/* 一律 503，
// 仅放行本端点（对齐原实现的「正在完成更新」语义）。
func Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if Active() && r.Method != http.MethodGet && r.Method != http.MethodHead &&
			r.Method != http.MethodOptions && strings.HasPrefix(r.URL.Path, "/api/") &&
			r.URL.Path != "/api/v1/system/update-ready" {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusServiceUnavailable)
			_ = json.NewEncoder(w).Encode(map[string]string{"detail": "正在完成更新，请稍后再试"})
			return
		}
		next.ServeHTTP(w, r)
	})
}
