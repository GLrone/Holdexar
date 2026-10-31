package secretbox

import (
	"context"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"

	"holdexar/server-go/internal/store"
)

// 全库密文换钥重加密 + 密钥防护模式切换编排。
//
// 唯一事实源清单（新增密文列必须登记到这里，否则换钥后读不出）：
//   - steam_accounts.cookies         用途 steam-cookies
//   - app_settings.value_json        用途 kv:<key>（account.steam_api_key 等凭据键）
//   - proxy_subscriptions.url        用途 proxy-subscription
//   - subscription_snapshots.url     用途 proxy-subscription
//   - proxies.username / .password   用途 proxy-credential
//
// 换钥协议（顺序不可调换）：先落新密钥文件（过渡记录要用新主密钥封装）→
// 落过渡记录 → 单事务全库重加密 → 成功删过渡记录 / 失败回滚密钥文件并删记录。
// 任意一步被强杀，库内密文要么旧（过渡记录可解）要么新（密钥文件可解）。

// 换钥触达的加密用途串。
const (
	PurposeSteamCookies      = "steam-cookies"
	PurposeProxySubscription = "proxy-subscription"
	PurposeProxyCredential   = "proxy-credential"
)

// SecretPurpose settings 凭据键的加密用途串（每键独立派生密钥）。
func SecretPurpose(key string) string {
	if key == "pilot.llm.provider_key.legacy-v1" {
		return "kv:pilot.llm.api_key"
	}
	return "kv:" + key
}

// Counts 全库换钥各表条数（键名与 Python 侧返回字典一致）。
type Counts struct {
	AccountCookies   int `json:"accountCookies"`
	SettingSecrets   int `json:"settingSecrets"`
	Subscriptions    int `json:"subscriptions"`
	Snapshots        int `json:"snapshots"`
	ProxyCredentials int `json:"proxyCredentials"`
}

// SwitchResult 模式切换结果。
type SwitchResult struct {
	Mode   string `json:"mode"`
	Counts Counts `json:"counts"`
}

// ResumeResult 启动续跑结果。
type ResumeResult struct {
	Resumed bool   `json:"resumed"`
	Reason  string `json:"reason,omitempty"`
	Counts  Counts `json:"counts"`
}

// ReencryptAll 库内全部敏感密文从 oldMaster 迁到 newMaster（单事务）。
func ReencryptAll(ctx context.Context, gate *store.WriteGate, oldMaster, newMaster []byte) (Counts, error) {
	var counts Counts
	err := gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		var err error
		counts, err = reencryptScan(ctx, conn, oldMaster, newMaster, false)
		return err
	})
	if err != nil {
		return Counts{}, err
	}
	return counts, nil
}

// migrateValue 单个密文值换钥；非密文 / 空值返回空串（调用方跳过，不写回）。
func migrateValue(token, purpose string, old, new []byte) (string, error) {
	if token == "" || !IsEncrypted(token) {
		return "", nil
	}
	plain, err := DecryptWith(token, purpose, old)
	if err != nil {
		return "", err
	}
	return EncryptWith(plain, purpose, new)
}

func reencryptScan(ctx context.Context, conn *sql.Conn, old, new []byte, tolerant bool) (Counts, error) {
	var counts Counts
	migrate := migrateValue
	if tolerant {
		migrate = func(token, purpose string, old, new []byte) (string, error) {
			out, err := migrateValue(token, purpose, old, new)
			if err != nil {
				return "", nil // 已是新密文的行用旧钥解不开属预期，逐行容忍
			}
			return out, nil
		}
	}

	// steam_accounts.cookies
	var cookieUpds []struct{ steamID, token string }
	if err := scanEach(ctx, conn, "SELECT steam_id, cookies FROM steam_accounts",
		func(scan func(...any) error) (bool, error) {
			var steamID, cookies string
			if err := scan(&steamID, &cookies); err != nil {
				return false, err
			}
			token, err := migrate(cookies, PurposeSteamCookies, old, new)
			if err != nil {
				return false, err
			}
			if token != "" {
				cookieUpds = append(cookieUpds, struct{ steamID, token string }{steamID, token})
			}
			return true, nil
		}); err != nil {
		return counts, err
	}
	for _, u := range cookieUpds {
		if _, err := conn.ExecContext(ctx, "UPDATE steam_accounts SET cookies = ? WHERE steam_id = ?", u.token, u.steamID); err != nil {
			return counts, err
		}
		counts.AccountCookies++
	}

	// app_settings.value_json（JSON 列：密文以 JSON 字符串落库）
	var kvUpds []struct{ key, raw string }
	if err := scanEach(ctx, conn, "SELECT key, value_json FROM app_settings",
		func(scan func(...any) error) (bool, error) {
			var key string
			var raw sql.NullString
			if err := scan(&key, &raw); err != nil {
				return false, err
			}
			if !raw.Valid {
				return true, nil
			}
			s, ok := jsonStringValue(raw.String)
			if !ok {
				return true, nil // 非字符串值（对象/数字）不属凭据语义
			}
			token, err := migrate(s, SecretPurpose(key), old, new)
			if err != nil {
				return false, err
			}
			if token != "" {
				kvUpds = append(kvUpds, struct{ key, raw string }{key, mustJSONString(token)})
			}
			return true, nil
		}); err != nil {
		return counts, err
	}
	for _, u := range kvUpds {
		if _, err := conn.ExecContext(ctx, "UPDATE app_settings SET value_json = ? WHERE key = ?", u.raw, u.key); err != nil {
			return counts, err
		}
		counts.SettingSecrets++
	}

	// proxy_subscriptions.url
	var subUpds []struct {
		id    int64
		token string
	}
	if err := scanEach(ctx, conn, "SELECT id, url FROM proxy_subscriptions",
		func(scan func(...any) error) (bool, error) {
			var id int64
			var url string
			if err := scan(&id, &url); err != nil {
				return false, err
			}
			token, err := migrate(url, PurposeProxySubscription, old, new)
			if err != nil {
				return false, err
			}
			if token != "" {
				subUpds = append(subUpds, struct {
					id    int64
					token string
				}{id, token})
			}
			return true, nil
		}); err != nil {
		return counts, err
	}
	for _, u := range subUpds {
		if _, err := conn.ExecContext(ctx, "UPDATE proxy_subscriptions SET url = ? WHERE id = ?", u.token, u.id); err != nil {
			return counts, err
		}
		counts.Subscriptions++
	}

	// subscription_snapshots.url
	var snapUpds []struct {
		id    int64
		token string
	}
	if err := scanEach(ctx, conn, "SELECT id, url FROM subscription_snapshots",
		func(scan func(...any) error) (bool, error) {
			var id int64
			var url sql.NullString
			if err := scan(&id, &url); err != nil {
				return false, err
			}
			token, err := migrate(url.String, PurposeProxySubscription, old, new)
			if err != nil {
				return false, err
			}
			if token != "" {
				snapUpds = append(snapUpds, struct {
					id    int64
					token string
				}{id, token})
			}
			return true, nil
		}); err != nil {
		return counts, err
	}
	for _, u := range snapUpds {
		if _, err := conn.ExecContext(ctx, "UPDATE subscription_snapshots SET url = ? WHERE id = ?", u.token, u.id); err != nil {
			return counts, err
		}
		counts.Snapshots++
	}

	// proxies.username / password
	type proxyUpd struct {
		id                 int64
		username, password string
		hasUser, hasPass   bool
	}
	var proxyRows []proxyUpd
	if err := scanEach(ctx, conn, "SELECT id, username, password FROM proxies",
		func(scan func(...any) error) (bool, error) {
			var id int64
			var username, password sql.NullString
			if err := scan(&id, &username, &password); err != nil {
				return false, err
			}
			row := proxyUpd{id: id}
			if username.Valid {
				token, err := migrate(username.String, PurposeProxyCredential, old, new)
				if err != nil {
					return false, err
				}
				if token != "" {
					row.username, row.hasUser = token, true
				}
			}
			if password.Valid {
				token, err := migrate(password.String, PurposeProxyCredential, old, new)
				if err != nil {
					return false, err
				}
				if token != "" {
					row.password, row.hasPass = token, true
				}
			}
			if row.hasUser || row.hasPass {
				proxyRows = append(proxyRows, row)
			}
			return true, nil
		}); err != nil {
		return counts, err
	}
	for _, u := range proxyRows {
		if u.hasUser {
			if _, err := conn.ExecContext(ctx, "UPDATE proxies SET username = ? WHERE id = ?", u.username, u.id); err != nil {
				return counts, err
			}
			counts.ProxyCredentials++
		}
		if u.hasPass {
			if _, err := conn.ExecContext(ctx, "UPDATE proxies SET password = ? WHERE id = ?", u.password, u.id); err != nil {
				return counts, err
			}
			counts.ProxyCredentials++
		}
	}
	return counts, nil
}

// scanEach 遍历查询结果；fn 返回 false 停止。
func scanEach(ctx context.Context, conn *sql.Conn, query string, fn func(scan func(...any) error) (bool, error)) error {
	rows, err := conn.QueryContext(ctx, query)
	if err != nil {
		return fmt.Errorf("换钥读取失败: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		more, err := fn(rows.Scan)
		if err != nil {
			return err
		}
		if !more {
			break
		}
	}
	return rows.Err()
}

// jsonStringValue JSON 文本顶层为字符串时返回其值。
func jsonStringValue(raw string) (string, bool) {
	var parsed any
	dec := json.NewDecoder(strings.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(&parsed); err != nil {
		return "", false
	}
	s, ok := parsed.(string)
	return s, ok
}

// ── 模式切换编排 ─────────────────────────────────────────

// SwitchMode 切换密钥防护模式并完成全库换钥。
// mode=legacy 换钥到机器派生主密钥后删除密钥文件；dpapi 仅 Windows；
// passphrase 返回后进程处于已解锁态。当前为口令模式且锁定时须提供
// currentPassphrase。失败时不改变库与密钥文件。
func SwitchMode(ctx context.Context, gate *store.WriteGate, dataDir, mode string, passphrase, currentPassphrase string) (SwitchResult, error) {
	switch mode {
	case ModeLegacy, ModeDPAPI, ModePassphrase:
	default:
		return SwitchResult{}, keyringErrf("未知的密钥模式：'%s'", mode)
	}

	oldMode, err := ModeOf(dataDir)
	if err != nil {
		return SwitchResult{}, err
	}
	if oldMode == ModePassphrase && currentUnlockedKey() == nil {
		ok := false
		if currentPassphrase != "" {
			ok, err = Unlock(dataDir, currentPassphrase)
			if err != nil {
				return SwitchResult{}, err
			}
		}
		if !ok {
			return SwitchResult{}, keyringErr("当前口令不正确或未提供，无法切换保护方式")
		}
	}

	oldMaster, err := ResolveDMK(dataDir)
	if err != nil {
		return SwitchResult{}, err
	}

	if mode == ModeLegacy {
		newMaster, err := LegacyMaster(dataDir)
		if err != nil {
			return SwitchResult{}, err
		}
		counts, err := reencryptWithStaging(ctx, gate, dataDir, oldMaster, newMaster, nil)
		if err != nil {
			return SwitchResult{}, err
		}
		if err := RemoveRecord(dataDir); err != nil {
			return SwitchResult{}, err
		}
		ResetState()
		return SwitchResult{Mode: mode, Counts: counts}, nil
	}

	newMaster := randBytes(keyBytes)
	var record map[string]any
	if mode == ModeDPAPI {
		record, err = WrapDPAPI(newMaster, dataDir)
	} else {
		if passphrase == "" {
			return SwitchResult{}, keyringErr("口令不能为空")
		}
		record, err = WrapPassphrase(newMaster, passphrase)
	}
	if err != nil {
		return SwitchResult{}, err
	}
	counts, err := reencryptWithStaging(ctx, gate, dataDir, oldMaster, newMaster, record)
	if err != nil {
		return SwitchResult{}, err
	}
	if mode == ModePassphrase {
		LoadUnlockedKey(newMaster) // 刚设的口令即当前会话口令
	}
	return SwitchResult{Mode: mode, Counts: counts}, nil
}

// reencryptWithStaging 按崩溃恢复协议执行：新密钥文件 → 过渡记录 → 换钥 → 清理。
// record 为新密钥文件内容；legacy 方向为 nil（新主密钥可由本机材料重算，
// legacy 兜底恒可用，该方向不落密钥文件）。
func reencryptWithStaging(ctx context.Context, gate *store.WriteGate, dataDir string, oldMaster, newMaster []byte, record map[string]any) (Counts, error) {
	oldRecord := SnapshotRecord(dataDir)
	staged := WrapStaging(oldMaster, newMaster)
	fail := func() {
		_ = RestoreRecord(dataDir, oldRecord)
		_ = RemoveStaging(dataDir)
	}
	if record != nil {
		if err := WriteRecord(dataDir, record); err != nil {
			fail()
			return Counts{}, err
		}
	}
	if err := WriteStaging(dataDir, staged); err != nil {
		fail()
		return Counts{}, err
	}
	counts, err := ReencryptAll(ctx, gate, oldMaster, newMaster)
	if err != nil {
		fail()
		return Counts{}, err
	}
	if err := RemoveStaging(dataDir); err != nil {
		return counts, err
	}
	return counts, nil
}

// ── 崩溃续跑 ─────────────────────────────────────────────

// MigrationPending 是否存在未收尾的换钥（崩溃遗留的过渡记录）。
func MigrationPending(dataDir string) bool {
	rec, err := ReadStaging(dataDir)
	return err != nil || rec != nil
}

// ResumePendingMigration 启动时收尾被打断的换钥。
// 过渡记录存在 ⇒ 库内密文只可能是旧的（换钥是单事务），用旧主密钥重跑换钥
// 到当前密钥即可，重跑幂等：已是新密文的行在解密阶段失败并被跳过。
// 口令模式下解不出旧密钥时保留记录等用户解锁，不做破坏性动作。
func ResumePendingMigration(ctx context.Context, gate *store.WriteGate, dataDir string, passphrase string) (ResumeResult, error) {
	if !MigrationPending(dataDir) {
		return ResumeResult{Resumed: false, Reason: "无待收尾的换钥"}, nil
	}

	oldMaster, err := StagingMaster(dataDir, passphrase)
	if err != nil {
		return ResumeResult{}, err // 过渡记录损坏：与 Python 一致按启动失败上浮
	}
	if oldMaster == nil {
		return ResumeResult{Resumed: false, Reason: "过渡记录需要口令解锁"}, nil
	}

	newMaster, err := ResolveDMK(dataDir)
	if err != nil {
		return ResumeResult{Resumed: false, Reason: "当前密钥不可用"}, nil
	}

	if hex.EncodeToString(oldMaster) == hex.EncodeToString(newMaster) {
		// 密钥文件根本没被替换（崩溃在写文件之前）——直接清记录即可
		if err := RemoveStaging(dataDir); err != nil {
			return ResumeResult{}, err
		}
		return ResumeResult{Resumed: true}, nil
	}

	counts, err := resumeReencrypt(ctx, gate, oldMaster, newMaster)
	if err != nil {
		return ResumeResult{}, err
	}
	if err := RemoveStaging(dataDir); err != nil {
		return ResumeResult{}, err
	}
	return ResumeResult{Resumed: true, Counts: counts}, nil
}

// resumeReencrypt 续跑换钥：逐行容忍「已经是新密文」的行。
func resumeReencrypt(ctx context.Context, gate *store.WriteGate, oldMaster, newMaster []byte) (Counts, error) {
	var counts Counts
	err := gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		var err error
		counts, err = reencryptScan(ctx, conn, oldMaster, newMaster, true)
		return err
	})
	if err != nil {
		return Counts{}, err
	}
	return counts, nil
}
