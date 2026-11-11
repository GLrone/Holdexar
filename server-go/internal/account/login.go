package account

import (
	"context"
	"errors"
	"strings"
	"sync"
	"time"
)

type LoginState struct {
	State         string `json:"state"`
	Message       string `json:"message"`
	Error         string `json:"error"`
	CodeHint      string `json:"code_hint"`
	CodeAvailable bool   `json:"code_available"`
	StartedAt     string `json:"started_at"`
	UpdatedAt     string `json:"updated_at"`
}

type loginAttempt struct {
	ctx       context.Context
	cancel    context.CancelFunc
	finish    func()
	infoMu    sync.Mutex
	auth      *loginAuth
	state     LoginState
	running   bool
	steamID   string
	codeGuard int
	interval  time.Duration
}

func (s *Service) newLoginState(state, message string) LoginState {
	stamp := s.now().Format("2006-01-02T15:04:05")
	return LoginState{State: state, Message: message, StartedAt: stamp, UpdatedAt: stamp}
}
func (s *Service) LoginStatus() LoginState {
	s.loginMu.Lock()
	defer s.loginMu.Unlock()
	return s.loginStatusLocked()
}
func (s *Service) loginStatusLocked() LoginState {
	if s.login == nil {
		return s.newLoginState("idle", "")
	}
	return s.login.state
}
func (s *Service) updateLogin(a *loginAttempt, state, message, failure string) bool {
	s.loginMu.Lock()
	defer s.loginMu.Unlock()
	if s.login != a || !a.running {
		return false
	}
	a.state.State, a.state.Message, a.state.Error = state, message, failure
	a.state.UpdatedAt = s.now().Format("2006-01-02T15:04:05")
	if state == "failed" || state == "done" || state == "idle" {
		a.running = false
	}
	return true
}
func loginErrorText(err error) string {
	var failure *loginFailure
	if errors.As(err, &failure) {
		return failure.message
	}
	return "登录收尾失败，请重试"
}
func (s *Service) failLogin(a *loginAttempt, err error) {
	if errors.Is(a.ctx.Err(), context.Canceled) {
		s.updateLogin(a, "idle", "", "")
		return
	}
	message := loginErrorText(err)
	if errors.Is(a.ctx.Err(), context.DeadlineExceeded) {
		message = "登录等待超时，请重试"
	}
	if s.updateLogin(a, "failed", "", message) {
		s.opts.Log("应用内账号密码登录未完成 │ 原因=%s", message)
	}
}
func (s *Service) StartLogin(parent context.Context, account, password string) (map[string]any, error) {
	s.loginMu.Lock()
	if s.login != nil && s.login.running {
		state := s.loginStatusLocked()
		s.loginMu.Unlock()
		return map[string]any{"ok": false, "busy": true, "state": state, "error": "已有登录进行中"}, nil
	}
	account = strings.TrimSpace(account)
	if account == "" || password == "" {
		state := s.loginStatusLocked()
		s.loginMu.Unlock()
		return map[string]any{"ok": false, "state": state, "error": "请填写账号与密码"}, nil
	}
	life, done, err := s.work(s.life)
	if err != nil {
		s.loginMu.Unlock()
		return nil, err
	}
	timeout := s.opts.LoginTimeout
	if timeout <= 0 {
		timeout = 5 * time.Minute
	}
	ctx, cancel := context.WithTimeout(life, timeout)
	a := &loginAttempt{ctx: ctx, cancel: cancel, state: s.newLoginState("signing", "正在登录…"), running: true}
	var finished sync.Once
	a.finish = func() {
		finished.Do(func() {
			cancel()
			a.infoMu.Lock()
			a.auth = nil
			a.infoMu.Unlock()
			done()
		})
	}
	s.login = a
	s.loginMu.Unlock()
	stopParent := context.AfterFunc(parent, cancel)
	auth, err := s.opts.Steam.beginLogin(ctx, account, password)
	stopParent()
	if err == nil {
		err = ctx.Err()
	}
	if err != nil {
		s.failLogin(a, err)
		a.finish()
		return map[string]any{"ok": false, "state": s.LoginStatus()}, nil
	}
	a.infoMu.Lock()
	a.auth = auth
	a.steamID, a.codeGuard, a.interval = auth.steamID, auth.codeGuard, auth.interval
	a.infoMu.Unlock()
	state, message := "awaiting_confirmation", ""
	confirmed := false
	for _, guard := range auth.guards {
		confirmed = confirmed || guard == 4 || guard == 5
	}
	if auth.deviceOK {
		message = "本机已获 Steam 信任，正在完成登录…"
	} else if !confirmed && auth.codeGuard != 0 {
		state = "awaiting_code"
	}
	s.loginMu.Lock()
	if s.login == a && a.running {
		a.state.CodeAvailable = auth.codeGuard != 0
		if auth.codeGuard == 2 {
			a.state.CodeHint = "email"
		} else if auth.codeGuard == 3 {
			a.state.CodeHint = "totp"
		}
	}
	s.loginMu.Unlock()
	if !s.updateLogin(a, state, message, "") {
		a.finish()
		return map[string]any{"ok": false, "state": s.LoginStatus()}, nil
	}
	go s.pollLoginAttempt(a)
	return map[string]any{"ok": true, "state": s.LoginStatus()}, nil
}
func (s *Service) pollLoginAttempt(a *loginAttempt) {
	defer a.finish()
	for {
		if err := s.opts.Steam.sleep(a.ctx, a.interval); err != nil {
			s.failLogin(a, err)
			return
		}
		a.infoMu.Lock()
		rejected, token, err := s.opts.Steam.pollLogin(a.ctx, a.auth)
		a.infoMu.Unlock()
		if err != nil {
			s.failLogin(a, err)
			return
		}
		if rejected {
			s.failLogin(a, &loginFailure{message: "本次登录未获确认（手机上已拒绝或已超时），请重新登录"})
			return
		}
		if token == "" {
			continue
		}
		if !s.updateLogin(a, "finalizing", "正在建立登录态…", "") {
			return
		}
		var cookies string
		for attempt := 0; attempt < 4; attempt++ {
			cookies, err = s.opts.Steam.WebCookies(a.ctx, token, a.steamID, nil)
			if err == nil {
				break
			}
			var wallet *WalletError
			if errors.As(err, &wallet) && wallet.Kind == "owner" || attempt == 3 || a.ctx.Err() != nil {
				s.failLogin(a, err)
				return
			}
			s.updateLogin(a, "finalizing", "网络不稳，正在自动恢复登录…", "")
			s.opts.Log("登录收尾遇网络中断，正在自动重试 │ 次数=%d", attempt+1)
			if err = s.opts.Steam.sleep(a.ctx, time.Duration(attempt+1)*2*time.Second); err != nil {
				s.failLogin(a, err)
				return
			}
		}
		if _, err = s.BindCookies(a.ctx, cookies); err != nil {
			s.failLogin(a, err)
			return
		}
		s.opts.Log("应用内登录成功并已绑定账号 │ SteamID=%s", a.steamID)
		s.updateLogin(a, "done", "登录成功", "")
		return
	}
}
func (s *Service) SubmitLoginCode(parent context.Context, code string) (map[string]any, error) {
	s.loginMu.Lock()
	a := s.login
	waiting := a != nil && a.running && (a.state.State == "awaiting_code" || a.state.State == "awaiting_confirmation") && a.codeGuard != 0
	s.loginMu.Unlock()
	if !waiting {
		return map[string]any{"ok": false, "no_session": true, "state": s.LoginStatus(), "error": "当前没有等待验证码的登录"}, nil
	}
	if strings.TrimSpace(code) == "" {
		return map[string]any{"ok": false, "state": s.LoginStatus(), "error": "请输入验证码"}, nil
	}
	ctx, done, err := s.work(parent)
	if err != nil {
		return nil, err
	}
	defer done()
	ctx, cancel := context.WithCancel(ctx)
	stop := context.AfterFunc(a.ctx, cancel)
	defer func() { stop(); cancel() }()
	a.infoMu.Lock()
	s.loginMu.Lock()
	waiting = s.login == a && a.running && (a.state.State == "awaiting_code" || a.state.State == "awaiting_confirmation")
	s.loginMu.Unlock()
	if waiting {
		err = s.opts.Steam.submitLoginGuard(ctx, a.auth, code)
	}
	a.infoMu.Unlock()
	if !waiting {
		return map[string]any{"ok": false, "no_session": true, "state": s.LoginStatus(), "error": "当前没有等待验证码的登录"}, nil
	}
	if err != nil {
		return map[string]any{"ok": false, "state": s.LoginStatus(), "error": loginErrorText(err)}, nil
	}
	s.loginMu.Lock()
	if s.login == a && a.running && (a.state.State == "awaiting_code" || a.state.State == "awaiting_confirmation") {
		a.state.State, a.state.Message, a.state.Error = "awaiting_confirmation", "验证码已接受，正在完成登录…", ""
		a.state.UpdatedAt = s.now().Format("2006-01-02T15:04:05")
	}
	s.loginMu.Unlock()
	return map[string]any{"ok": true, "state": s.LoginStatus()}, nil
}
func (s *Service) CancelLogin() map[string]any {
	s.loginMu.Lock()
	defer s.loginMu.Unlock()
	if a := s.login; a != nil && a.running {
		a.cancel()
		a.running = false
		a.state.State, a.state.Message, a.state.Error = "idle", "", ""
		a.state.UpdatedAt = s.now().Format("2006-01-02T15:04:05")
	}
	return map[string]any{"ok": true, "state": s.loginStatusLocked()}
}
