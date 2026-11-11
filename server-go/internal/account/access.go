package account

import (
	"context"
	"database/sql"
	"errors"
)

type StoreSession struct {
	SteamID, Cookie string
	bound           sql.NullString
	version         string
}

var ErrNoStoreCredentials = errors.New("账号无可用 Steam Cookie")

func (session StoreSession) Check(ctx context.Context, conn *sql.Conn) error {
	var present int
	err := conn.QueryRowContext(ctx, `SELECT 1 FROM steam_accounts WHERE steam_id=? AND cookies=? AND CAST(bound_at AS TEXT) IS ?`, session.SteamID, session.version, session.bound).Scan(&present)
	if errors.Is(err, sql.ErrNoRows) {
		return walletError("changed", "账号已解绑或登录信息已更新，本轮结果已跳过")
	}
	return err
}

func (s *Service) StoreSession(parent context.Context, sid string, force bool) (StoreSession, error) {
	ctx, done, err := s.work(parent)
	if err != nil {
		return StoreSession{}, err
	}
	defer done()
	if sid == "" {
		row, found, err := s.row(ctx, "")
		if err != nil {
			return StoreSession{}, err
		}
		if !found {
			return StoreSession{}, ErrAccountNotFound
		}
		sid = row.ID
	}
	return s.boundStoreSession(ctx, sid, force)
}

func (s *Service) OwnedToken(parent context.Context, sid string) (string, error) {
	return s.apiToken(parent, sid, true)
}
func (s *Service) BoundToken(parent context.Context, sid string) (string, error) {
	return s.apiToken(parent, sid, false)
}

func (s *Service) HasStoredToken(ctx context.Context, sid string) (bool, error) {
	row, found, err := s.row(ctx, sid)
	if err != nil || !found {
		return false, err
	}
	raw, err := s.credential(row)
	if err != nil || !credentialOwnerMatches(raw, sid) {
		return false, nil
	}
	_, token := cookieToken(ParseCookies(raw)["steamLoginSecure"])
	return token != "", nil
}

func (s *Service) apiToken(parent context.Context, sid string, allowViewer bool) (string, error) {
	ctx, done, e := s.work(parent)
	if e != nil {
		return "", e
	}
	defer done()
	row, found, e := s.row(ctx, sid)
	if e != nil {
		return "", e
	}
	if !found {
		if !allowViewer {
			return "", ErrAccountNotFound
		}
		rows, e := s.rows(ctx)
		if e != nil || len(rows) == 0 {
			return "", e
		}
		row = rows[0]
	}
	session, e := s.boundStoreSession(ctx, row.ID, false)
	if errors.Is(e, ErrAccountNotFound) {
		return "", nil
	}
	if errors.Is(e, ErrNoStoreCredentials) {
		return "", walletError("owner", "账号登录凭据与请求身份不一致")
	}
	if e != nil {
		return "", e
	}
	_, token := cookieToken(ParseCookies(session.Cookie)["steamLoginSecure"])
	return token, nil
}

func (s *Service) boundStoreSession(ctx context.Context, sid string, force bool) (StoreSession, error) {
	unlock, e := s.lock(ctx, sid)
	if e != nil {
		return StoreSession{}, e
	}
	defer unlock()
	row, found, e := s.row(ctx, sid)
	if e != nil {
		return StoreSession{}, e
	}
	if !found {
		return StoreSession{}, ErrAccountNotFound
	}
	session := StoreSession{SteamID: row.ID, bound: row.Bound}
	raw, e := s.credential(row)
	if e != nil {
		return session, e
	}
	if ParseCookies(raw)["steamLoginSecure"] == "" {
		return session, ErrNoStoreCredentials
	}
	if !credentialOwnerMatches(raw, row.ID) {
		return session, walletError("owner", "账号登录凭据与请求身份不一致")
	}
	raw, e = s.liveLocked(ctx, &row, raw, force)
	if e != nil {
		return session, e
	}
	if !credentialOwnerMatches(raw, row.ID) {
		return session, walletError("owner", "账号登录凭据与请求身份不一致")
	}
	if CookieFreshness(raw, s.now()).Expired {
		return session, walletError("expired", expiredMessage)
	}
	session.Cookie, session.version = raw, row.Cookies
	return session, nil
}
