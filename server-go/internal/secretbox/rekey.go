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

// 加密列事实源清单（新增密文列须同步登记，否则换钥后无法解密）：
//   - steam_accounts.cookies         (steam-cookies)
//   - app_settings.value_json        (kv:<key>)
//   - app_settings[plugins.registry] (plugin:<id>:<field>)
//   - proxy_subscriptions.url        (proxy-subscription)
//   - subscription_snapshots.url     (proxy-subscription)
//   - proxies.username / .password   (proxy-credential)
// 协议顺序：写新密钥文件 → 写过渡记录 → 单事务全库重加密 → 成功删过渡记录 / 失败回滚文件并删记录。

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

type Counts struct {
	AccountCookies   int `json:"accountCookies"`
	SettingSecrets   int `json:"settingSecrets"`
	Subscriptions    int `json:"subscriptions"`
	Snapshots        int `json:"snapshots"`
	ProxyCredentials int `json:"proxyCredentials"`
}

type SwitchResult struct {
	Mode   string `json:"mode"`
	Counts Counts `json:"counts"`
}

type ResumeResult struct {
	Resumed bool   `json:"resumed"`
	Reason  string `json:"reason,omitempty"`
	Counts  Counts `json:"counts"`
}

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

	var kvUpds []struct{ key, raw string }
	if err := scanEach(ctx, conn, "SELECT key, value_json FROM app_settings",
		func(scan func(...any) error) (bool, error) {
			var key string
			var raw sql.NullString
			if err := scan(&key, &raw); err != nil {
				return false, err
			}
			if key == "plugins.registry" {
				updated, err := migratePluginRegistry(raw.String, old, new, migrate)
				if err != nil {
					return false, err
				}
				if updated != "" {
					kvUpds = append(kvUpds, struct{ key, raw string }{key, updated})
				}
				return true, nil
			}
			if !raw.Valid {
				return true, nil
			}
			s, ok := jsonStringValue(raw.String)
			if !ok {
				return true, nil
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

func migratePluginRegistry(raw string, old, new []byte, migrate func(string, string, []byte, []byte) (string, error)) (string, error) {
	var entries []map[string]json.RawMessage
	if err := json.Unmarshal([]byte(raw), &entries); err != nil {
		return "", fmt.Errorf("插件配置换钥读取失败: %w", err)
	}
	changed := false
	for _, entry := range entries {
		var manifest map[string]json.RawMessage
		if err := json.Unmarshal(entry["manifest"], &manifest); err != nil {
			return "", fmt.Errorf("插件配置声明无效: %w", err)
		}
		var id string
		if err := json.Unmarshal(manifest["id"], &id); err != nil || id == "" {
			return "", fmt.Errorf("插件配置标识无效")
		}
		var config map[string]json.RawMessage
		if value, ok := entry["config"]; ok {
			if err := json.Unmarshal(value, &config); err != nil {
				return "", fmt.Errorf("插件配置内容无效: %w", err)
			}
		}
		configChanged := false
		for field, value := range config {
			token, ok := jsonStringValue(string(value))
			if !ok || !IsEncrypted(token) {
				continue
			}
			updated, err := migrate(token, "plugin:"+id+":"+field, old, new)
			if err != nil {
				return "", fmt.Errorf("插件凭据换钥失败: %w", err)
			}
			if updated != "" {
				config[field] = json.RawMessage(mustJSONString(updated))
				configChanged = true
			}
		}
		if configChanged {
			encoded, err := json.Marshal(config)
			if err != nil {
				return "", err
			}
			entry["config"] = encoded
			changed = true
		}
	}
	if !changed {
		return "", nil
	}
	encoded, err := json.Marshal(entries)
	return string(encoded), err
}

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

// SwitchMode 切换密钥模式并全库重加密（legacy 完成后删密钥文件；dpapi 仅 Windows；passphrase 返回已解锁态）。
// 当前为 passphrase 模式时（含已解锁态）必须验证 currentPassphrase；失败不改库与密钥文件。
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
	if oldMode == ModePassphrase {
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
		LoadUnlockedKey(newMaster)
	} else {
		ResetState()
	}
	return SwitchResult{Mode: mode, Counts: counts}, nil
}

// reencryptWithStaging 执行崩溃安全换钥；切往 legacy 时 record 为 nil（不落密钥文件）。
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

func MigrationPending(dataDir string) bool {
	rec, err := ReadStaging(dataDir)
	return err != nil || rec != nil
}

// ResumePendingMigration 续跑中断的换钥：用过渡记录旧主密钥容错重加密至当前主密钥；未解锁时保留记录。
func ResumePendingMigration(ctx context.Context, gate *store.WriteGate, dataDir string, passphrase string) (ResumeResult, error) {
	if !MigrationPending(dataDir) {
		return ResumeResult{Resumed: false, Reason: "无待收尾的换钥"}, nil
	}

	oldMaster, err := StagingMaster(dataDir, passphrase)
	if err != nil {
		return ResumeResult{}, err
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
