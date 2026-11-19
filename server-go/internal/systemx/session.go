package systemx

import (
	"crypto/rand"
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

// UnlockDeps 局域网解锁依赖（由装配层注入，未提供时解锁端点回 404）。
type UnlockDeps struct {
	Enabled func() bool
	// Verifier 返回 PHC 验证器串；空串＝未设密码，解锁拒绝。
	Verifier    func() (string, error)
	FailPeer    func(string)
	SuccessPeer func(string)
	// 保留兼容字段，运行时统一按对端 IP 记账。
	Fail    func()
	Success func()
}

func isSafeNextRedirect(next string) bool {
	if !strings.HasPrefix(next, "/") {
		return false
	}
	if strings.HasPrefix(next, "//") || strings.HasPrefix(next, `/\`) {
		return false
	}
	for i := 0; i < len(next); i++ {
		if next[i] < 0x20 || next[i] == 0x7f {
			return false
		}
	}
	return true
}

func recordUnlockFail(deps UnlockDeps, peer string) {
	if deps.FailPeer != nil {
		deps.FailPeer(peer)
		return
	}
	access.RecordUnlockFailureFor(peer)
}

func recordUnlockSuccess(deps UnlockDeps, peer string) {
	if deps.SuccessPeer != nil {
		deps.SuccessPeer(peer)
		return
	}
	access.RecordUnlockSuccessFor(peer)
}

// RegisterSession 注册引导票据、开发态会话（devSession=false 时 404）、登出与局域网解锁端点。
func RegisterSession(mux *http.ServeMux, gate *access.Gate, devSession bool, unlock ...UnlockDeps) {
	mux.HandleFunc("GET /api/v1/session/bootstrap", func(w http.ResponseWriter, r *http.Request) {
		if !consumeBootstrap(r.URL.Query().Get("nonce")) {
			writeJSON(w, http.StatusUnauthorized, map[string]any{"detail": "引导票据无效或已过期"})
			return
		}
		// 只允许站内相对路径，拒绝 //、/\ 与控制字符，避免开放跳转。
		target := r.URL.Query().Get("next")
		if !isSafeNextRedirect(target) {
			target = "/"
		}
		gate.Issue(w)
		w.Header().Set("Location", target)
		w.WriteHeader(http.StatusSeeOther)
	})
	mux.HandleFunc("GET /api/v1/session/dev", func(w http.ResponseWriter, r *http.Request) {
		// dev 通道仅据对端物理地址判定回环，不信任可伪造的 Host 头。
		if !devSession || !access.PeerIsLoopback(r.RemoteAddr) {
			writeJSON(w, http.StatusNotFound, map[string]any{"detail": "Not Found"})
			return
		}
		gate.Issue(w)
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
	})
	mux.HandleFunc("POST /api/v1/session/logout", func(w http.ResponseWriter, r *http.Request) {
		if gate != nil {
			gate.RevokeSession(w, r)
		}
		w.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("POST /api/v1/session/unlock", func(w http.ResponseWriter, r *http.Request) {
		var deps UnlockDeps
		if len(unlock) > 0 {
			deps = unlock[0]
		}
		if deps.Enabled == nil || !deps.Enabled() {
			writeJSON(w, http.StatusNotFound, map[string]any{"detail": "Not Found"})
			return
		}
		peer := access.PeerKey(r.RemoteAddr)
		if locked := access.UnlockLockedSeconds(peer); locked > 0 {
			w.Header().Set("Retry-After", fmt.Sprintf("%.0f", locked))
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
		verifier, err := deps.Verifier()
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]any{"detail": "访问密码读取失败"})
			return
		}
		if verifier == "" {
			recordUnlockFail(deps, peer)
			writeJSON(w, http.StatusUnauthorized, map[string]any{"detail": "访问密码未设置，请先在本机设置访问密码"})
			return
		}
		if !access.VerifyPassword(strings.TrimSpace(password), verifier) {
			recordUnlockFail(deps, peer)
			writeJSON(w, http.StatusUnauthorized, map[string]any{"detail": "访问密码不正确"})
			return
		}
		recordUnlockSuccess(deps, peer)
		gate.Issue(w)
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
	})
}
