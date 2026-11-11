package account

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	"holdexar/server-go/internal/jobrun"
	"holdexar/server-go/internal/monitoring"
	"holdexar/server-go/internal/secretbox"
	"holdexar/server-go/internal/store"
	"holdexar/server-go/internal/wishlist"
)

const credentialPurpose = "steam-cookies"
const legacyCookiesKey = "account.steam_cookies"
const expiredMessage = "Steam 登录已过期，请重新登录"
const renewFailedMessage = "Steam 登录已过期，正在自动续期（失败会自动重试）"

type ServiceOptions struct {
	DataDir      string
	Steam        *SteamClient
	Now          func() time.Time
	Log          func(string, ...any)
	Sleep        func(context.Context, time.Duration) error
	Recorder     *jobrun.Recorder
	Pool         *wishlist.Pool
	LoginTimeout time.Duration
	AfterBind    func(string)
	APIKey       func(context.Context) (string, error)
	Summaries    func(context.Context, []string, string) (map[string]wishlist.PlayerSummary, error)
}

type InputError struct{ Message string }

func (e *InputError) Error() string { return e.Message }

var ErrAccountNotFound = errors.New("账号不存在")

type Service struct {
	db       *sql.DB
	gate     *store.WriteGate
	opts     ServiceOptions
	life     context.Context
	cancel   context.CancelFunc
	mu       sync.Mutex
	closing  bool
	jobs     sync.WaitGroup
	locks    map[string]chan struct{}
	renewAt  map[string]time.Time
	lastSeen time.Time
	loginMu  sync.Mutex
	login    *loginAttempt
}

func NewService(parent context.Context, db *sql.DB, gate *store.WriteGate, opts ServiceOptions) *Service {
	if opts.Now == nil {
		opts.Now = time.Now
	}
	if opts.Log == nil {
		opts.Log = func(string, ...any) {}
	}
	if opts.Steam == nil {
		opts.Steam = NewSteamClient(SteamOptions{})
	}
	if opts.Pool == nil {
		opts.Pool = wishlist.NewPool(db, gate, monitoring.New(db, gate))
	}
	life, cancel := context.WithCancel(parent)
	return &Service{db: db, gate: gate, opts: opts, life: life, cancel: cancel, locks: map[string]chan struct{}{}, renewAt: map[string]time.Time{}}
}
func (s *Service) now() time.Time      { return s.opts.Now().In(accountZone) }
func accountStamp(at time.Time) string { return at.Format("2006-01-02 15:04:05.000000") }
func accountISO(at time.Time) string   { return at.Format("2006-01-02T15:04:05.000000") }
func (s *Service) work(ctx context.Context) (context.Context, func(), error) {
	s.mu.Lock()
	if s.closing {
		s.mu.Unlock()
		return nil, nil, context.Canceled
	}
	s.jobs.Add(1)
	s.mu.Unlock()
	ctx, cancel := context.WithCancel(ctx)
	stop := context.AfterFunc(s.life, cancel)
	return ctx, func() { stop(); cancel(); s.jobs.Done() }, nil
}
func (s *Service) lock(ctx context.Context, sid string) (func(), error) {
	s.mu.Lock()
	ch := s.locks[sid]
	if ch == nil {
		ch = make(chan struct{}, 1)
		s.locks[sid] = ch
	}
	s.mu.Unlock()
	select {
	case ch <- struct{}{}:
		return func() { <-ch }, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}
func (s *Service) MarkSeen() { s.mu.Lock(); s.lastSeen = s.now(); s.mu.Unlock() }
func (s *Service) Close(ctx context.Context) error {
	s.mu.Lock()
	s.closing = true
	s.cancel()
	s.mu.Unlock()
	done := make(chan struct{})
	go func() { s.jobs.Wait(); close(done) }()
	select {
	case <-done:
		s.opts.Steam.Close()
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

type accountRow struct {
	ID, Cookies, Persona, Avatar, InGame, Error string
	Wallet                                      map[string]any
	Active, Online, Frozen                      bool
	Bound, Checked, OnlineChecked               sql.NullString
	Backoff, Streak                             int
}

func (s *Service) rows(ctx context.Context, ids ...string) ([]accountRow, error) {
	query := `SELECT steam_id,cookies,persona_name,avatar_url,wallet_json,is_active,is_online,in_game,CAST(bound_at AS TEXT),CAST(wallet_checked_at AS TEXT),CAST(online_checked_at AS TEXT),wallet_error,wallet_backoff_level,wallet_fail_streak,wallet_frozen FROM steam_accounts`
	args := []any{}
	if len(ids) > 0 {
		query += ` WHERE steam_id=?`
		args = append(args, ids[0])
	}
	rows, e := s.db.QueryContext(ctx, query+` ORDER BY bound_at,steam_id`, args...)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	out := []accountRow{}
	for rows.Next() {
		var row accountRow
		var wallet sql.NullString
		if e = rows.Scan(&row.ID, &row.Cookies, &row.Persona, &row.Avatar, &wallet, &row.Active, &row.Online, &row.InGame, &row.Bound, &row.Checked, &row.OnlineChecked, &row.Error, &row.Backoff, &row.Streak, &row.Frozen); e != nil {
			return nil, e
		}
		if wallet.Valid {
			_ = json.Unmarshal([]byte(wallet.String), &row.Wallet)
		}
		out = append(out, row)
	}
	return out, rows.Err()
}
func (s *Service) row(ctx context.Context, sid string) (accountRow, bool, error) {
	var rows []accountRow
	var e error
	if sid != "" {
		rows, e = s.rows(ctx, sid)
	} else {
		rows, e = s.rows(ctx)
	}
	if e != nil {
		return accountRow{}, false, e
	}
	if sid != "" {
		for _, row := range rows {
			if row.ID == sid {
				return row, true, nil
			}
		}
		return accountRow{}, false, nil
	}
	for _, row := range rows {
		if row.Active {
			return row, true, nil
		}
	}
	if len(rows) > 0 {
		return rows[0], true, nil
	}
	return accountRow{}, false, nil
}
func (s *Service) credential(row accountRow) (string, error) {
	if !strings.HasPrefix(row.Cookies, secretbox.Prefix) {
		return row.Cookies, nil
	}
	value, e := secretbox.DecryptSecretIn(s.opts.DataDir, row.Cookies, credentialPurpose)
	if e != nil {
		return "", walletError("auth", "账号凭据无法在本机解密，请重新登录")
	}
	return value, nil
}
func (s *Service) freshness(row accountRow) Freshness {
	value, e := s.credential(row)
	if e != nil {
		return Freshness{Expired: true, Expiring: true}
	}
	f := CookieFreshness(value, s.now())
	if row.Error == expiredMessage {
		f.Expired = true
		f.Expiring = true
	}
	return f
}
func (s *Service) boundID(ctx context.Context) (string, error) {
	var raw sql.NullString
	e := s.db.QueryRowContext(ctx, `SELECT value_json FROM app_settings WHERE key='account.steam_id'`).Scan(&raw)
	if errors.Is(e, sql.ErrNoRows) {
		return "", nil
	}
	if e != nil {
		return "", e
	}
	var value string
	if raw.Valid {
		_ = json.Unmarshal([]byte(raw.String), &value)
	}
	return value, nil
}
func publicTime(raw sql.NullString) any {
	if !raw.Valid || raw.String == "" {
		return nil
	}
	return strings.Replace(raw.String, " ", "T", 1)
}
func (s *Service) views(ctx context.Context, rows []accountRow) ([]map[string]any, error) {
	ids := []string{}
	for _, row := range rows {
		ids = append(ids, row.ID)
	}
	counts, e := wishlistCounts(ctx, s.db, ids)
	if e != nil {
		return nil, e
	}
	bills := map[string]any{}
	if len(ids) > 0 {
		raw, _ := json.Marshal(ids)
		items, e := s.db.QueryContext(ctx, `SELECT steam_id,CAST(MAX(imported_at) AS TEXT) FROM bill_imports WHERE steam_id IN(SELECT value FROM json_each(?)) GROUP BY steam_id`, string(raw))
		if e != nil {
			return nil, e
		}
		for items.Next() {
			var id string
			var at sql.NullString
			if e = items.Scan(&id, &at); e != nil {
				items.Close()
				return nil, e
			}
			bills[id] = publicTime(at)
		}
		e = items.Err()
		items.Close()
		if e != nil {
			return nil, e
		}
	}
	out := []map[string]any{}
	for i, row := range rows {
		f := s.freshness(row)
		count := counts[row.ID]
		out = append(out, map[string]any{"steam_id": row.ID, "friend_code": FriendCode(row.ID), "persona_name": row.Persona, "avatar_url": row.Avatar, "is_active": row.Active, "is_primary": i == 0, "bound_at": publicTime(row.Bound), "wallet": row.Wallet, "wallet_error": row.Error, "wallet_frozen": row.Frozen, "session_expires_at": f.ExpiresAt, "session_expired": f.Expired, "session_has_refresh": f.HasRefresh, "wishlist_count": count.wishlist, "game_count": count.owned, "bills_synced_at": bills[row.ID], "redeem_used": 0, "is_online": row.Online, "in_game": row.InGame})
	}
	return out, nil
}
func (s *Service) Accounts(ctx context.Context) ([]map[string]any, error) {
	rows, e := s.rows(ctx)
	if e != nil {
		return nil, e
	}
	return s.views(ctx, rows)
}
func (s *Service) Status(ctx context.Context) (map[string]any, error) {
	rows, e := s.rows(ctx)
	if e != nil {
		return nil, e
	}
	views, e := s.views(ctx, rows)
	if e != nil {
		return nil, e
	}
	bound, e := s.boundID(ctx)
	if e != nil {
		return nil, e
	}
	status := map[string]any{"has_cookie": false, "cookie_steam_id": "", "bound_steam_id": bound, "mismatch": false, "profile": nil, "wallet": nil, "sync_error": "", "message": "", "accounts": views, "primary_steam_id": "", "session_expires_at": nil, "session_expired": false, "session_has_refresh": false, "is_online": false, "in_game": ""}
	if len(rows) == 0 {
		return status, nil
	}
	row := rows[0]
	for _, candidate := range rows {
		if candidate.Active {
			row = candidate
			break
		}
	}
	f := s.freshness(row)
	status["has_cookie"] = row.Cookies != ""
	status["cookie_steam_id"] = row.ID
	status["mismatch"] = bound != "" && bound != row.ID
	status["wallet"] = row.Wallet
	status["sync_error"] = row.Error
	status["primary_steam_id"] = rows[0].ID
	status["session_expires_at"] = f.ExpiresAt
	status["session_expired"] = f.Expired
	status["session_has_refresh"] = f.HasRefresh
	status["is_online"] = row.Online
	status["in_game"] = row.InGame
	if row.Persona != "" || row.Avatar != "" {
		status["profile"] = map[string]any{"steam_id": row.ID, "persona_name": row.Persona, "avatar_url": row.Avatar}
	}
	return status, nil
}
func (s *Service) BindCookies(parent context.Context, raw string) (map[string]any, error) {
	clean := FilterCookies(raw)
	sid := CookieSteamID(clean)
	if sid == "" {
		return nil, &InputError{"无法从 Cookie 解析 SteamID64，请确认复制的是登录后的完整 Cookie"}
	}
	if !credentialOwnerMatches(clean, sid) {
		return nil, &InputError{"账号登录凭据与 Steam 身份不一致"}
	}
	if owner, _ := cookieToken(ParseCookies(clean)["steamRefresh_steam"]); owner != "" && owner != sid {
		return nil, &InputError{"续期凭据与 Steam 账号不一致"}
	}
	ctx, done, e := s.work(parent)
	if e != nil {
		return nil, e
	}
	defer done()
	unlock, e := s.lock(ctx, sid)
	if e != nil {
		return nil, e
	}
	defer unlock()
	cipher, e := secretbox.EncryptSecretIn(s.opts.DataDir, clean, credentialPurpose)
	if e != nil {
		return nil, e
	}
	e = s.gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		if _, e := conn.ExecContext(ctx, `UPDATE steam_accounts SET is_active=0 WHERE steam_id!=? AND is_active=1`, sid); e != nil {
			return e
		}
		_, e := conn.ExecContext(ctx, `INSERT INTO steam_accounts(steam_id,cookies,persona_name,avatar_url,is_active,is_online,in_game,bound_at,wallet_error,wallet_backoff_level,wallet_fail_streak,wallet_frozen) VALUES(?,?,'','',1,0,'',?,'',0,0,0) ON CONFLICT(steam_id) DO UPDATE SET cookies=excluded.cookies,is_active=1,wallet_backoff_level=0,wallet_fail_streak=0,wallet_frozen=0`, sid, cipher, accountStamp(s.now()))
		if e != nil {
			return e
		}
		_, e = conn.ExecContext(ctx, `INSERT INTO tracked_accounts(steamid,kinds_json,created_at) VALUES(?,'{"wishlist":true,"owned":true}',?) ON CONFLICT(steamid) DO NOTHING`, sid, accountStamp(s.now()))
		return e
	})
	if e != nil {
		return nil, e
	}
	s.mu.Lock()
	delete(s.renewAt, sid)
	s.mu.Unlock()
	if s.opts.AfterBind != nil {
		s.opts.AfterBind(sid)
	}
	row, ok, e := s.row(ctx, sid)
	if e != nil {
		return nil, e
	}
	if !ok {
		return nil, fmt.Errorf("账号绑定未保存")
	}
	if _, e = s.syncLocked(ctx, row, true); e != nil {
		return nil, e
	}
	status, e := s.Status(ctx)
	if e != nil {
		return nil, e
	}
	if status["mismatch"] == true {
		status["message"] = "Cookie 中的 SteamID 与已保存的 SteamID64 不一致，请核对账号"
	} else if RefreshToken(clean) == "" {
		status["message"] = "未检测到续期凭据：登录 Steam 时请勾选「记住我」，否则约一天后需重新登录"
	}
	s.opts.Log("Steam 账号绑定已保存 │ SteamID=%s", sid)
	return status, nil
}
func (s *Service) SetActive(ctx context.Context, sid string) (map[string]any, error) {
	ctx, done, e := s.work(ctx)
	if e != nil {
		return nil, e
	}
	defer done()
	e = s.gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		var n int
		if e := conn.QueryRowContext(ctx, `SELECT count(*) FROM steam_accounts WHERE steam_id=?`, sid).Scan(&n); e != nil {
			return e
		}
		if n == 0 {
			return fmt.Errorf("%w: %s", ErrAccountNotFound, sid)
		}
		_, e := conn.ExecContext(ctx, `UPDATE steam_accounts SET is_active=(steam_id=?)`, sid)
		return e
	})
	if e != nil {
		return nil, e
	}
	return s.Status(ctx)
}
func (s *Service) liveLocked(ctx context.Context, row *accountRow, raw string, force bool) (string, error) {
	f := CookieFreshness(raw, s.now())
	if !f.HasRefresh || (!force && !f.Expiring) {
		return raw, nil
	}
	s.mu.Lock()
	last := s.renewAt[row.ID]
	if !last.IsZero() && s.now().Sub(last) < 10*time.Minute {
		s.mu.Unlock()
		return raw, nil
	}
	s.renewAt[row.ID] = s.now()
	s.mu.Unlock()
	renewed, e := s.opts.Steam.RefreshSession(ctx, raw)
	if e != nil {
		if ctx.Err() != nil {
			return "", ctx.Err()
		}
		s.opts.Log("账号登录态续期未完成，本轮保留原凭据 │ SteamID=%s，原因=%v", row.ID, e)
		return raw, nil
	}
	cipher, e := secretbox.EncryptSecretIn(s.opts.DataDir, renewed, credentialPurpose)
	if e != nil {
		return "", e
	}
	e = s.gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		result, e := conn.ExecContext(ctx, `UPDATE steam_accounts SET cookies=? WHERE steam_id=? AND cookies=?`, cipher, row.ID, row.Cookies)
		if e != nil {
			return e
		}
		n, e := result.RowsAffected()
		if e == nil && n == 0 {
			return walletError("changed", "账号登录信息已更新，本轮结果已跳过")
		}
		return e
	})
	if e != nil {
		return "", e
	}
	row.Cookies = cipher
	return renewed, nil
}
func (s *Service) saveWallet(ctx context.Context, row accountRow, snapshot map[string]any, message string, force, frozen bool) error {
	backoff, streak := 0, 0
	if snapshot == nil {
		snapshot = map[string]any{}
		for k, v := range row.Wallet {
			snapshot[k] = v
		}
		okAt := snapshot["ok_at"]
		if okAt == nil && snapshot["check_ok"] == true {
			okAt = snapshot["checked_at"]
		}
		snapshot["check_ok"] = false
		snapshot["error"] = message
		snapshot["checked_at"] = accountISO(s.now())
		snapshot["ok_at"] = okAt
		if !force {
			backoff = min(row.Backoff+1, 4)
			streak = row.Streak + 1
			frozen = frozen || streak >= 10
		}
	}
	raw, e := json.Marshal(snapshot)
	if e != nil {
		return e
	}
	return s.gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		result, e := conn.ExecContext(ctx, `UPDATE steam_accounts SET wallet_json=?,wallet_error=?,wallet_checked_at=?,wallet_backoff_level=?,wallet_fail_streak=?,wallet_frozen=? WHERE steam_id=? AND cookies=?`, string(raw), string([]rune(message)[:min(300, len([]rune(message)))]), accountStamp(s.now()), backoff, streak, frozen, row.ID, row.Cookies)
		if e != nil {
			return e
		}
		n, e := result.RowsAffected()
		if e == nil && n == 0 {
			return walletError("changed", "账号登录信息已更新，本轮余额已跳过")
		}
		return e
	})
}
func (s *Service) syncLocked(ctx context.Context, row accountRow, force bool) (map[string]any, error) {
	raw, e := s.credential(row)
	if e != nil {
		return map[string]any{"ok": false, "status": "no_cookie", "error": e.Error()}, nil
	}
	if CookieSteamID(raw) != row.ID {
		return map[string]any{"ok": false, "status": "no_cookie", "error": "尚未绑定可用的 Steam Cookie"}, nil
	}
	raw, e = s.liveLocked(ctx, &row, raw, false)
	if e != nil {
		return nil, e
	}
	f := CookieFreshness(raw, s.now())
	if f.Expired {
		message, status := expiredMessage, "session_expired"
		if f.HasRefresh {
			message, status = renewFailedMessage, "session_renew_failed"
		}
		if e = s.saveWallet(ctx, row, nil, message, force, !f.HasRefresh); e != nil {
			return nil, e
		}
		return map[string]any{"ok": false, "status": status, "error": message}, nil
	}
	wallet, fetchErr := s.opts.Steam.FetchWallet(ctx, raw)
	var target *WalletError
	if errors.As(fetchErr, &target) && target.Kind == "expired" {
		fresh, e := s.liveLocked(ctx, &row, raw, true)
		if e != nil {
			return nil, e
		}
		if fresh != raw {
			raw = fresh
			wallet, fetchErr = s.opts.Steam.FetchWallet(ctx, raw)
		}
		if errors.As(fetchErr, &target) && target.Kind == "expired" && RefreshToken(raw) == "" {
			if e = s.saveWallet(ctx, row, nil, expiredMessage, force, true); e != nil {
				return nil, e
			}
			return map[string]any{"ok": false, "status": "session_expired", "error": expiredMessage}, nil
		}
	}
	if fetchErr != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		if e = s.saveWallet(ctx, row, nil, fetchErr.Error(), force, false); e != nil {
			return nil, e
		}
		s.syncProfile(ctx, row)
		return map[string]any{"ok": false, "error": fetchErr.Error()}, nil
	}
	snapshot := map[string]any{"balance": wallet.Balance, "balance_display": wallet.BalanceDisplay, "currency_code": wallet.CurrencyCode, "currency_symbol": wallet.CurrencySymbol, "currency_id": wallet.CurrencyID, "region_code": "", "country_code": "", "steam_id": wallet.SteamID, "checked_at": accountISO(s.now()), "ok_at": accountISO(s.now()), "check_ok": true, "error": ""}
	if e = s.saveWallet(ctx, row, snapshot, "", force, false); e != nil {
		return nil, e
	}
	s.syncProfile(ctx, row)
	return map[string]any{"ok": true, "cached": false, "wallet": snapshot}, nil
}
func (s *Service) SyncWallet(parent context.Context, sid string, force bool) (map[string]any, error) {
	ctx, done, e := s.work(parent)
	if e != nil {
		return nil, e
	}
	defer done()
	result, e := s.syncWallet(ctx, sid, force)
	if errors.Is(e, ErrAccountNotFound) {
		return map[string]any{"ok": false, "status": "no_cookie", "error": "账号已解绑"}, nil
	}
	return result, e
}

func (s *Service) syncWallet(ctx context.Context, sid string, force bool) (map[string]any, error) {
	if sid == "" {
		row, ok, e := s.row(ctx, "")
		if e != nil {
			return nil, e
		}
		if !ok {
			return map[string]any{"ok": false, "status": "no_cookie", "error": "尚未绑定 Steam Cookie"}, nil
		}
		sid = row.ID
	}
	unlock, e := s.lock(ctx, sid)
	if e != nil {
		return nil, e
	}
	defer unlock()
	row, ok, e := s.row(ctx, sid)
	if e != nil {
		return nil, e
	}
	if !ok {
		return nil, ErrAccountNotFound
	}
	return s.syncLocked(ctx, row, force)
}

func (s *Service) RefreshAccount(parent context.Context, sid string) (map[string]any, error) {
	sid = strings.TrimSpace(sid)
	if !numericID(sid) {
		return nil, &InputError{"steam_id 需为数字 SteamID64"}
	}
	ctx, done, err := s.work(parent)
	if err != nil {
		return nil, err
	}
	defer done()
	wallet, err := s.syncWallet(ctx, sid, false)
	if errors.Is(err, ErrAccountNotFound) {
		return nil, fmt.Errorf("%w: %s", err, sid)
	}
	if err != nil {
		return nil, err
	}
	delete(wallet, "cached")
	queued := []string{}
	if s.opts.AfterBind != nil {
		s.opts.AfterBind(sid)
		queued = []string{"wishlist", "family", "bills"}
	}
	return map[string]any{"ok": wallet["ok"] == true, "steamId": sid, "wallet": wallet, "queued": queued}, nil
}
