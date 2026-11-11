package account

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"maps"
	"strings"

	"holdexar/server-go/internal/secretbox"
	"holdexar/server-go/internal/store"
)

type legacyQueryer interface {
	QueryContext(context.Context, string, ...any) (*sql.Rows, error)
}

func legacyValues(ctx context.Context, q legacyQueryer) (map[string]sql.NullString, error) {
	rows, e := q.QueryContext(ctx, `SELECT key,value_json FROM app_settings WHERE key IN('account.steam_cookies','account.wallet_snapshot','account.profile')`)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	values := map[string]sql.NullString{}
	for rows.Next() {
		var key string
		var value sql.NullString
		if e = rows.Scan(&key, &value); e != nil {
			return nil, e
		}
		values[key] = value
	}
	return values, rows.Err()
}

func (s *Service) MigrateLegacyKV(ctx context.Context) (int, error) {
	values, e := legacyValues(ctx, s.db)
	if e != nil {
		return 0, e
	}
	var raw string
	if value := values[legacyCookiesKey]; value.Valid {
		if e = json.Unmarshal([]byte(value.String), &raw); e != nil {
			return 0, fmt.Errorf("旧账号凭据设置格式无效")
		}
	}
	sid := CookieSteamID(raw)
	if sid == "" {
		return 0, nil
	}
	cipher, e := secretbox.EncryptSecretIn(s.opts.DataDir, raw, credentialPurpose)
	if e != nil {
		return 0, e
	}
	profile := map[string]any{}
	_ = json.Unmarshal([]byte(values["account.profile"].String), &profile)
	var wallet any
	var walletSnapshot map[string]any
	if value := values["account.wallet_snapshot"]; value.Valid {
		wallet = value.String
		_ = json.Unmarshal([]byte(value.String), &walletSnapshot)
	}
	migrated := 0
	e = s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		current, e := legacyValues(ctx, conn)
		if e != nil {
			return e
		}
		if !maps.Equal(values, current) {
			return fmt.Errorf("旧账号设置已更新，本轮接管已跳过")
		}
		inserted, e := conn.ExecContext(ctx, `INSERT INTO steam_accounts(steam_id,cookies,persona_name,avatar_url,wallet_json,is_active,is_online,in_game,bound_at,wallet_error,wallet_backoff_level,wallet_fail_streak,wallet_frozen)
 VALUES(?,?,?,?,?,NOT EXISTS(SELECT 1 FROM steam_accounts WHERE is_active=1),0,'',?,'',0,0,0) ON CONFLICT(steam_id) DO NOTHING`, sid, cipher, textField(profile["persona_name"]), textField(profile["avatar_url"]), wallet, accountStamp(s.now()))
		if e != nil {
			return e
		}
		n, e := inserted.RowsAffected()
		if e != nil {
			return e
		}
		migrated = int(n)
		if migrated > 0 {
			for _, snapshot := range []map[string]any{walletSnapshot, profile} {
				if owner := textField(snapshot["steam_id"]); owner != "" && owner != sid {
					return fmt.Errorf("旧账号快照与登录凭据属主不一致，原数据已保留")
				}
			}
			if _, e = conn.ExecContext(ctx, `INSERT INTO tracked_accounts(steamid,kinds_json,created_at) VALUES(?,'{"wishlist":true,"owned":true}',?) ON CONFLICT(steamid) DO NOTHING`, sid, accountStamp(s.now())); e != nil {
				return e
			}
		}
		_, e = conn.ExecContext(ctx, `UPDATE app_settings SET value_json=CASE WHEN key=? THEN '""' ELSE 'null' END,updated_at=? WHERE key IN('account.steam_cookies','account.wallet_snapshot','account.profile')`, legacyCookiesKey, accountStamp(s.now()))
		return e
	})
	if e != nil {
		return 0, e
	}
	s.opts.Log("旧账号设置已接管 │ SteamID=%s，新增=%d", sid, migrated)
	return migrated, nil
}

func (s *Service) SealCredentials(ctx context.Context) (int, error) {
	rows, e := s.db.QueryContext(ctx, `SELECT steam_id,cookies FROM steam_accounts`)
	if e != nil {
		return 0, e
	}
	updates := [][3]string{}
	for rows.Next() {
		var sid, raw string
		if e = rows.Scan(&sid, &raw); e != nil {
			rows.Close()
			return 0, e
		}
		if raw != "" && !strings.HasPrefix(raw, secretbox.Prefix) {
			updates = append(updates, [3]string{sid, raw, ""})
		}
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return 0, e
	}
	for i := range updates {
		updates[i][2], e = secretbox.EncryptSecretIn(s.opts.DataDir, updates[i][1], credentialPurpose)
		if e != nil {
			return 0, e
		}
	}
	if len(updates) == 0 {
		return 0, nil
	}
	raw, _ := json.Marshal(updates)
	sealed := 0
	e = s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		result, e := conn.ExecContext(ctx, `UPDATE steam_accounts AS a SET cookies=json_extract(u.value,'$[2]') FROM json_each(?) u WHERE a.steam_id=json_extract(u.value,'$[0]') AND a.cookies=json_extract(u.value,'$[1]')`, string(raw))
		if e != nil {
			return e
		}
		n, e := result.RowsAffected()
		sealed = int(n)
		return e
	})
	if e != nil {
		return 0, e
	}
	s.opts.Log("存量账号凭据已加密保存 │ 条数=%d", sealed)
	return sealed, nil
}
