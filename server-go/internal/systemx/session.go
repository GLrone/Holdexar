// 会话发放：一次性引导票据 + 会话 Cookie。令牌只在进程内存里，页面无法
// 凭空取得——桌面壳走引导票据（进程内传递），开发态走 dev 通道（打包态
// 完全没有这条入口，发行版没有任何「本机进程可自助取会话」的途径）。
package systemx

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"

	"holdexar/server-go/internal/access"
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

// UnlockDeps 局域网自助解锁的依赖面：开关与密码解析由装配层注入（密码
// 事实源在设置密文 KV 与环境变量，systemx 不反向依赖 settings）。nil 依赖
// 时解锁端点恒 404（局域网模式未接线）。
type UnlockDeps struct {
	Enabled  func() bool
	Password func() (string, error)
	Fail     func()
	Success  func()
}

// RegisterSession 注册会话发放端点。devSession 控制开发态通道：关闭时
// dev 端点回 404——发行版没有「本机进程可自助取会话」的入口。
// unlock 局域网解锁依赖：缺省时解锁端点恒 404（默认回环-only 行为不变）。
func RegisterSession(mux *http.ServeMux, gate *access.Gate, devSession bool, unlock ...UnlockDeps) {
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
	mux.HandleFunc("POST /api/v1/session/unlock", func(w http.ResponseWriter, r *http.Request) {
		var deps UnlockDeps
		if len(unlock) > 0 {
			deps = unlock[0]
		}
		// 仅局域网模式存在此入口；默认模式语义与未注册路由一致（404）。
		if deps.Enabled == nil || !deps.Enabled() {
			writeJSON(w, http.StatusNotFound, map[string]any{"detail": "Not Found"})
			return
		}
		if locked := access.UnlockLockedSeconds(); locked > 0 {
			writeJSON(w, http.StatusTooManyRequests, map[string]any{"detail": fmt.Sprintf("尝试过于频繁，请 %d 秒后再试", int(locked)+1)})
			return
		}
		raw, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
		var body map[string]any
		if err != nil || json.Unmarshal(raw, &body) != nil {
			writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": "请求体无效"})
			return
		}
		password, ok := body["password"].(string)
		if !ok {
			writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": "请求体无效"})
			return
		}
		expected, err := deps.Password()
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]any{"detail": "访问密码读取失败"})
			return
		}
		// 密码常数时间比较；连续失败达阈值后全局限速（防局域网内穷举）。
		if expected == "" || subtle.ConstantTimeCompare([]byte(strings.TrimSpace(password)), []byte(expected)) != 1 {
			deps.Fail()
			writeJSON(w, http.StatusUnauthorized, map[string]any{"detail": "访问密码不正确"})
			return
		}
		deps.Success()
		gate.Issue(w)
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
	})
}
