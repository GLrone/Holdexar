package main

// 忘记密码找回（5 个端点均在 access.PublicPaths）：登录启动与完成时均核验 Steam 账号已绑定后再签发会话。
// 攻击面控制：/login/start、/login/code 与 /complete 校对端维度解锁限速，/cancel 限 1s 冷却。

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"sync"
	"time"

	"holdexar/server-go/internal/access"
	"holdexar/server-go/internal/account"
)

var (
	recoveryCancelMu   sync.Mutex
	lastRecoveryCancel = time.Now().Add(-time.Second)
)

func registerRecovery(mux *http.ServeMux, accountService *account.Service, db *sql.DB, gate *access.Gate, logLine func(string, ...any)) {
	boundSetCheck := func(steamID string) error {
		var n int
		if err := db.QueryRow(`SELECT COUNT(*) FROM steam_accounts WHERE steam_id=?`, steamID).Scan(&n); err != nil {
			return fmt.Errorf("绑定账号核对失败：%v", err)
		}
		if n == 0 {
			return fmt.Errorf("该 Steam 账号未绑定到本应用，无法用于找回访问密码")
		}
		return nil
	}
	write := func(w http.ResponseWriter, payload any) {
		var buf bytes.Buffer
		enc := json.NewEncoder(&buf)
		enc.SetEscapeHTML(false)
		if err := enc.Encode(payload); err != nil {
			http.Error(w, "响应序列化失败", http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(bytes.TrimSuffix(buf.Bytes(), []byte{'\n'}))
	}

	mux.HandleFunc("POST /api/v1/session/recovery/login/start", func(w http.ResponseWriter, r *http.Request) {
		peer := access.PeerKey(r.RemoteAddr)
		if remaining := access.UnlockLockedSeconds(peer); remaining > 0 {
			write(w, map[string]any{"ok": false, "error": fmt.Sprintf("尝试次数过多，%d 秒后可重试", int(remaining)+1)})
			return
		}
		var body struct {
			Account  string `json:"account"`
			Password string `json:"password"`
		}
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8192)).Decode(&body); err != nil {
			access.RecordUnlockFailureFor(peer)
			write(w, map[string]any{"ok": false, "error": "请求体无法读取"})
			return
		}
		res, err := accountService.StartRecoveryLogin(r.Context(), body.Account, body.Password, boundSetCheck, func() {
			access.RecordUnlockFailureFor(peer)
		})
		if err != nil {
			access.RecordUnlockFailureFor(peer)
			write(w, map[string]any{"ok": false, "error": "找回登录启动失败"})
			return
		}
		if res["ok"] == false && res["busy"] != true {
			access.RecordUnlockFailureFor(peer)
		}
		write(w, res)
	})

	mux.HandleFunc("POST /api/v1/session/recovery/login/code", func(w http.ResponseWriter, r *http.Request) {
		peer := access.PeerKey(r.RemoteAddr)
		if remaining := access.UnlockLockedSeconds(peer); remaining > 0 {
			write(w, map[string]any{"ok": false, "error": fmt.Sprintf("尝试次数过多，%d 秒后可重试", int(remaining)+1)})
			return
		}
		var body struct {
			Code string `json:"code"`
		}
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8192)).Decode(&body); err != nil {
			access.RecordUnlockFailureFor(peer)
			write(w, map[string]any{"ok": false, "error": "请求体无法读取"})
			return
		}
		res, err := accountService.SubmitRecoveryLoginCode(r.Context(), body.Code)
		if err != nil {
			access.RecordUnlockFailureFor(peer)
			write(w, map[string]any{"ok": false, "error": "找回验证码提交失败"})
			return
		}
		if res["ok"] == false {
			access.RecordUnlockFailureFor(peer)
		}
		write(w, res)
	})

	mux.HandleFunc("GET /api/v1/session/recovery/login/status", func(w http.ResponseWriter, r *http.Request) {
		write(w, accountService.LoginStatus())
	})

	mux.HandleFunc("POST /api/v1/session/recovery/cancel", func(w http.ResponseWriter, r *http.Request) {
		recoveryCancelMu.Lock()
		if time.Since(lastRecoveryCancel) < time.Second {
			recoveryCancelMu.Unlock()
			write(w, map[string]any{"ok": true})
			return
		}
		lastRecoveryCancel = time.Now()
		recoveryCancelMu.Unlock()
		accountService.CancelRecoveryLogin()
		write(w, map[string]any{"ok": true})
	})

	mux.HandleFunc("POST /api/v1/session/recovery/complete", func(w http.ResponseWriter, r *http.Request) {
		peer := access.PeerKey(r.RemoteAddr)
		if remaining := access.UnlockLockedSeconds(peer); remaining > 0 {
			write(w, map[string]any{"ok": false, "error": fmt.Sprintf("尝试次数过多，%d 秒后可重试", int(remaining)+1)})
			return
		}
		steamID, done := accountService.ConsumeRecoverySteamID()
		if !done {
			access.RecordUnlockFailureFor(peer)
			write(w, map[string]any{"ok": false, "error": "Steam 登录流程未完成"})
			return
		}
		if err := boundSetCheck(steamID); err != nil {
			access.RecordUnlockFailureFor(peer)
			write(w, map[string]any{"ok": false, "error": err.Error()})
			return
		}
		gate.Issue(w)
		access.RecordUnlockSuccessFor(peer)
		logLine("找回完成：Steam 登录身份通过绑定核验，已签发会话")
		write(w, map[string]any{"ok": true})
	})
}
