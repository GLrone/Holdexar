// 会话发放：一次性引导票据 + 会话 Cookie。令牌只在进程内存里，页面无法
// 凭空取得——桌面壳走引导票据（进程内传递），开发态走 dev 通道（打包态
// 完全没有这条入口，发行版没有任何「本机进程可自助取会话」的途径）。
package systemx

import (
	"crypto/rand"
	"encoding/base64"
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

// RegisterSession 注册会话发放端点。devSession 控制开发态通道：关闭时
// dev 端点回 404——发行版没有「本机进程可自助取会话」的入口。
func RegisterSession(mux *http.ServeMux, gate *access.Gate, devSession bool) {
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
}
