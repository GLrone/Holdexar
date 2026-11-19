// Package updateguard 提供升级验收期的启动校验、API 写保护与页面就绪标记。
package updateguard

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"time"

	"holdexar/server-go/internal/access"
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
	attemptID   atomic.Value
	token       atomic.Value
	pageReady   atomic.Bool
	dataDirPath atomic.Value
}

var current state

func Active() bool { return current.protected.Load() }

func AttemptID() string {
	value, _ := current.attemptID.Load().(string)
	return value
}

func PageReady() bool { return current.pageReady.Load() }

func DataDir() string {
	value, _ := current.dataDirPath.Load().(string)
	return value
}

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

// validateStartup 无 update 标记与环境时直接放行，否则在 3s 窗口内校验事务登记并核对令牌。
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

func SetPageReady(attemptID string) error {
	if !Active() || AttemptID() == "" || AttemptID() != attemptID {
		return ErrNotDuringCheck
	}
	current.pageReady.Store(true)
	return nil
}

// Token 返回当前升级验收期的数据保护令牌（未激活时为空）。
func Token() string {
	value, _ := current.token.Load().(string)
	return value
}

func dbPathOf(dataDir string) string {
	return filepath.Join(dataDir, "holdexar.db")
}

func ClearProtection() {
	current.protected.Store(false)
	current.pageReady.Store(false)
}

func HandleUpdateReady(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	if !access.PeerIsLoopback(r.RemoteAddr) {
		w.WriteHeader(http.StatusForbidden)
		_ = json.NewEncoder(w).Encode(map[string]string{"detail": "该操作仅限本机执行"})
		return
	}
	if r.Body == nil {
		w.WriteHeader(http.StatusBadRequest)
		_ = json.NewEncoder(w).Encode(map[string]string{"detail": "请求体无效"})
		return
	}
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8192))
	var body map[string]any
	if err := dec.Decode(&body); err != nil || body == nil {
		w.WriteHeader(http.StatusBadRequest)
		_ = json.NewEncoder(w).Encode(map[string]string{"detail": "请求体无效"})
		return
	}
	var trailing any
	if err := dec.Decode(&trailing); err != io.EOF {
		w.WriteHeader(http.StatusBadRequest)
		_ = json.NewEncoder(w).Encode(map[string]string{"detail": "请求体无效"})
		return
	}
	attemptID, _ := body["attemptId"].(string)
	if err := SetPageReady(attemptID); err != nil {
		w.WriteHeader(http.StatusConflict)
		_ = json.NewEncoder(w).Encode(map[string]string{"detail": "当前不在更新验收期间"})
		return
	}
	_ = json.NewEncoder(w).Encode(map[string]bool{"ready": true})
}

// Middleware 验收期内拦截非安全方法的 /api/* 请求并返回 503，仅放行 update-ready。
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
