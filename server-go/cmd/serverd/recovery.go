package main

// 忘记密码找回：锁定态下用 Steam 官方登录证明身份。
//
// 信任锚 = Steam 官方认证产生的登录身份 ∈ 数据库绑定账号集合（登录流程在
// BindCookies 之前经一次性守卫校验，未绑定账号不落库不建登录态）。通过后
// 签发本机会话，用户随后在设置页修改局域网访问密码。
//
// 攻击面控制：四个端点全部公开可达（找回发生在锁定态、无会话），共用
// 局域网解锁限速器——同一条口令防线，任一通道穷举都触发全局锁定；Steam
// 凭据中转面被 busy 互斥 + 完成态校验压缩（每次只有一条登录流）。

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"holdexar/server-go/internal/access"
	"holdexar/server-go/internal/account"
)

var lastRecoveryCancel = time.Now().Add(-time.Second)

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
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(payload)
	}

	mux.HandleFunc("POST /api/v1/session/recovery/login/start", func(w http.ResponseWriter, r *http.Request) {
		if remaining := access.UnlockLockedSeconds(); remaining > 0 {
			write(w, map[string]any{"ok": false, "error": fmt.Sprintf("尝试次数过多，%.0f 秒后可重试", remaining)})
			return
		}
		var body struct {
			Account  string `json:"account"`
			Password string `json:"password"`
		}
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8192)).Decode(&body); err != nil {
			write(w, map[string]any{"ok": false, "error": "请求体无法读取"})
			return
		}
		// 守卫在登录成功、绑定落库之前校验账号归属（一次性）
		accountService.SetRecoveryGuard(boundSetCheck)
		res, err := accountService.StartLogin(r.Context(), body.Account, body.Password)
		if err != nil {
			write(w, map[string]any{"ok": false, "error": "找回登录启动失败"})
			return
		}
		write(w, res)
	})

	mux.HandleFunc("POST /api/v1/session/recovery/login/code", func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Code string `json:"code"`
		}
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8192)).Decode(&body); err != nil {
			write(w, map[string]any{"ok": false, "error": "请求体无法读取"})
			return
		}
		res, err := accountService.SubmitLoginCode(r.Context(), body.Code)
		if err != nil {
			write(w, map[string]any{"ok": false, "error": "找回验证码提交失败"})
			return
		}
		write(w, res)
	})

	mux.HandleFunc("GET /api/v1/session/recovery/login/status", func(w http.ResponseWriter, r *http.Request) {
		write(w, accountService.LoginStatus())
	})

	mux.HandleFunc("POST /api/v1/session/recovery/cancel", func(w http.ResponseWriter, r *http.Request) {
		// 冷却 1 秒：免认证端点不可被无成本高频重置（打断进行中的找回）
		if time.Since(lastRecoveryCancel) < time.Second {
			write(w, map[string]any{"ok": true})
			return
		}
		lastRecoveryCancel = time.Now()
		accountService.CancelLogin()
		// 回复最小化：状态机详情（验证码提示等）不进免认证响应——
		// 前端取消后经 status 轮询取状态即可
		write(w, map[string]any{"ok": true})
	})

	mux.HandleFunc("POST /api/v1/session/recovery/complete", func(w http.ResponseWriter, r *http.Request) {
		if remaining := access.UnlockLockedSeconds(); remaining > 0 {
			write(w, map[string]any{"ok": false, "error": fmt.Sprintf("尝试次数过多，%.0f 秒后可重试", remaining)})
			return
		}
		steamID, done := accountService.RecoverySteamID()
		if !done {
			write(w, map[string]any{"ok": false, "error": "Steam 登录流程未完成"})
			return
		}
		_ = steamID // 身份经守卫核验（绑定集合成员）；会话签发不再回显身份
		gate.Issue(w)
		access.RecordUnlockSuccess()
		logLine("找回完成：Steam 登录身份通过绑定核验，已签发会话")
		write(w, map[string]any{"ok": true})
	})
}
