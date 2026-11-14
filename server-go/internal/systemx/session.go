// 会话发放：一次性引导票据 + 会话 Cookie。令牌只在进程内存里，页面无法
// 凭空取得——桌面壳走引导票据（进程内传递），开发态走 dev 通道（打包态
// 完全没有这条入口），局域网模式走访问密码自助解锁（/session/unlock）。
package systemx

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"holdexar/server-go/internal/access"
	"holdexar/server-go/internal/secretbox"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
)

// 引导票据：nonce → 过期时刻。单次使用，取出即删；过期条目在生成时顺带清理。
const bootstrapTTL = 60 * time.Second

var (
	bootstrapMu   sync.Mutex
	bootstrapPool = map[string]time.Time{}
)

func randomNonce() string {
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		panic("引导票据生成失败：" + err.Error())
	}
	return base64.RawURLEncoding.EncodeToString(buf)
}

// CreateBootstrap 生成一次性引导票据（桌面壳进程内调用，不经网络）。
func CreateBootstrap() string {
	bootstrapMu.Lock()
	defer bootstrapMu.Unlock()
	now := time.Now()
	for key, expiry := range bootstrapPool {
		if !expiry.After(now) {
			delete(bootstrapPool, key)
		}
	}
	nonce := randomNonce()
	bootstrapPool[nonce] = now.Add(bootstrapTTL)
	return nonce
}

func consumeBootstrap(nonce string) bool {
	bootstrapMu.Lock()
	expiry, ok := bootstrapPool[nonce]
	delete(bootstrapPool, nonce)
	bootstrapMu.Unlock()
	return ok && expiry.After(time.Now())
}

// UnlockDeps 自助解锁通道的持久化依赖；DB 为 nil 时解锁端点恒 404。
type UnlockDeps struct {
	DB        *sql.DB
	WriteGate *store.WriteGate
	DataDir   string
}

// RegisterSession 注册会话发放端点。devSession 控制开发态通道：关闭时
// dev 端点回 404——发行版没有「本机进程可自助取会话」的入口。
func RegisterSession(mux *http.ServeMux, gate *access.Gate, devSession bool, unlock UnlockDeps) {
	mux.HandleFunc("GET /api/v1/session/bootstrap", func(w http.ResponseWriter, r *http.Request) {
		if !consumeBootstrap(r.URL.Query().Get("nonce")) {
			writeJSON(w, http.StatusUnauthorized, map[string]any{"detail": "引导票据无效或已过期"})
			return
		}
		// 只允许站内相对路径，避免变成开放跳转。
		target := r.URL.Query().Get("next")
		if !strings.HasPrefix(target, "/") || strings.HasPrefix(target, "//") {
			target = "/"
		}
		gate.Issue(w)
		w.Header().Set("Location", target)
		w.WriteHeader(http.StatusSeeOther)
	})
	mux.HandleFunc("GET /api/v1/session/dev", func(w http.ResponseWriter, r *http.Request) {
		if !devSession {
			writeJSON(w, http.StatusNotFound, map[string]any{"detail": "Not Found"})
			return
		}
		gate.Issue(w)
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
	})
	// 局域网自助解锁：访问密码换会话 Cookie（仅局域网模式存在此入口）。
	mux.HandleFunc("POST /api/v1/session/unlock", func(w http.ResponseWriter, r *http.Request) {
		if unlock.DB == nil || !access.IsEnabled() {
			writeJSON(w, http.StatusNotFound, map[string]any{"detail": "Not Found"})
			return
		}
		if locked := access.UnlockLockedSeconds(); locked > 0 {
			writeJSON(w, http.StatusTooManyRequests, map[string]any{"detail": fmt.Sprintf("尝试过于频繁，请 %d 秒后再试", int(locked)+1)})
			return
		}
		var body struct {
			Password *string `json:"password"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.Password == nil {
			writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": "请求体需要 password 字段"})
			return
		}
		expected, err := EnsureLANPassword(r.Context(), unlock)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]any{"detail": "访问密码读取失败"})
			return
		}
		// 密码常数时间比较；连续失败达阈值后全局限速，防局域网内穷举。
		if subtle.ConstantTimeCompare([]byte(strings.TrimSpace(*body.Password)), []byte(expected)) != 1 {
			access.UnlockRecordFailure()
			writeJSON(w, http.StatusUnauthorized, map[string]any{"detail": "访问密码不正确"})
			return
		}
		access.UnlockRecordSuccess()
		gate.Issue(w)
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
	})
}

// EnsureLANPassword 访问密码解析：环境变量优先，其次设置凭据 KV；都没有则
// 生成并落库（加密凭据，与 Python settings_service 同口径）。
func EnsureLANPassword(ctx context.Context, unlock UnlockDeps) (string, error) {
	if pw := access.EnvPassword(); pw != "" {
		return pw, nil
	}
	stored, err := settings.ReadSecret(ctx, unlock.DB, unlock.DataDir, settings.LANPasswordKey)
	if err != nil {
		return "", err
	}
	if stored != "" {
		return stored, nil
	}
	generated := access.GeneratePassword()
	sealed, err := secretbox.EncryptSecretIn(unlock.DataDir, generated, secretbox.SecretPurpose(settings.LANPasswordKey))
	if err != nil {
		return "", err
	}
	err = unlock.WriteGate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		return settings.WriteValue(ctx, conn, settings.LANPasswordKey, sealed)
	})
	if err != nil {
		return "", err
	}
	return generated, nil
}

// InitLANAccess 局域网访问模式启动初始化：读设置开关并同步 Host 闸白名单，
// 开启时确保访问密码已存在。失败按关闭处理（fail-closed）。
func InitLANAccess(ctx context.Context, unlock UnlockDeps) (bool, error) {
	enabled, err := readLANEnabled(ctx, unlock.DB)
	if err == nil {
		lanMode := access.SetEnabled(enabled)
		access.RefreshExtraHosts()
		if !lanMode {
			return false, nil
		}
		if _, err = EnsureLANPassword(ctx, unlock); err == nil {
			return true, nil
		}
	}
	access.SetEnabled(false)
	access.RefreshExtraHosts()
	return false, err
}

// readLANEnabled 读设置开关；存量布尔落库恒为 JSON true/false，异常值按
// 初始化失败走 fail-closed。
func readLANEnabled(ctx context.Context, db *sql.DB) (bool, error) {
	var raw sql.NullString
	err := db.QueryRowContext(ctx,
		"SELECT value_json FROM app_settings WHERE key = ?", settings.LANEnabledKey).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	if !raw.Valid {
		return false, nil
	}
	var enabled bool
	if err = json.Unmarshal([]byte(raw.String), &enabled); err != nil {
		return false, err
	}
	return enabled, nil
}
