package secretbox

import (
	"context"
	"database/sql"
	"encoding/json"
	"path/filepath"
	"testing"

	"holdexar/server-go/internal/store"
)

// rekeyEnv 建最小换钥环境：隔离库 + 写闸 + 五张密文表（列名与线上一致）。
func rekeyEnv(t *testing.T) (dir string, gate *store.WriteGate, db *sql.DB) {
	t.Helper()
	dir = t.TempDir()
	dbPath := filepath.Join(dir, "holdexar.db")
	db, err := store.Open(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	schema := []string{
		`CREATE TABLE steam_accounts (steam_id TEXT PRIMARY KEY, cookies TEXT NOT NULL DEFAULT '')`,
		`CREATE TABLE app_settings (key TEXT PRIMARY KEY, value_json JSON, updated_at TEXT)`,
		`CREATE TABLE proxy_subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL)`,
		`CREATE TABLE subscription_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT)`,
		`CREATE TABLE proxies (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, password TEXT)`,
	}
	for _, ddl := range schema {
		if _, err := db.Exec(ddl); err != nil {
			t.Fatal(err)
		}
	}
	return dir, store.NewWriteGate(db, dbPath), db
}

// seedCiphertexts 以指定主密钥写全链样值；返回各明文以便换钥后核对。
func seedCiphertexts(t *testing.T, db *sql.DB, master []byte) map[string]string {
	t.Helper()
	must := func(token string, err error) string {
		if err != nil {
			t.Fatal(err)
		}
		return token
	}
	cookie := must(EncryptWith("cookie-plain", PurposeSteamCookies, master))
	apiKey := must(EncryptWith("api-key-plain", SecretPurpose("account.steam_api_key"), master))
	legacyKey := must(EncryptWith("legacy-llm-plain", SecretPurpose("pilot.llm.provider_key.legacy-v1"), master))
	subURL := must(EncryptWith("sub-url-plain", PurposeProxySubscription, master))
	snapURL := must(EncryptWith("snap-url-plain", PurposeProxySubscription, master))
	proxyUser := must(EncryptWith("proxy-user-plain", PurposeProxyCredential, master))
	proxyPass := must(EncryptWith("proxy-pass-plain", PurposeProxyCredential, master))
	exec := func(q string, args ...any) {
		if _, err := db.Exec(q, args...); err != nil {
			t.Fatal(err)
		}
	}
	exec(`INSERT INTO steam_accounts (steam_id, cookies) VALUES ('sid-1', ?)`, cookie)
	exec(`INSERT INTO steam_accounts (steam_id, cookies) VALUES ('sid-plain', 'plaintext-cookie')`)
	exec(`INSERT INTO steam_accounts (steam_id, cookies) VALUES ('sid-empty', '')`)
	exec(`INSERT INTO app_settings (key, value_json) VALUES ('account.steam_api_key', ?)`, mustJSONString(apiKey))
	exec(`INSERT INTO app_settings (key, value_json) VALUES ('pilot.llm.provider_key.legacy-v1', ?)`, mustJSONString(legacyKey))
	exec(`INSERT INTO app_settings (key, value_json) VALUES ('games.derived_inputs', '{"series": {}}')`)
	exec(`INSERT INTO app_settings (key, value_json) VALUES ('ui.theme', '"dark"')`)
	exec(`INSERT INTO proxy_subscriptions (url) VALUES (?)`, subURL)
	exec(`INSERT INTO proxy_subscriptions (url) VALUES ('plain-url')`)
	exec(`INSERT INTO subscription_snapshots (url) VALUES (?)`, snapURL)
	exec(`INSERT INTO proxies (username, password) VALUES (?, ?)`, proxyUser, proxyPass)
	return map[string]string{
		"cookie": cookie, "cookiePlain": "cookie-plain",
		"apiKey": apiKey, "apiKeyPlain": "api-key-plain",
		"legacyKey": legacyKey, "legacyKeyPlain": "legacy-llm-plain",
		"subURL": subURL, "subURLPlain": "sub-url-plain",
		"snapURL": snapURL, "snapURLPlain": "snap-url-plain",
		"proxyUser": proxyUser, "proxyUserPlain": "proxy-user-plain",
		"proxyPass": proxyPass, "proxyPassPlain": "proxy-pass-plain",
	}
}

func dbScalar(t *testing.T, db *sql.DB, q string, args ...any) string {
	t.Helper()
	var s string
	if err := db.QueryRow(q, args...).Scan(&s); err != nil {
		t.Fatal(err)
	}
	return s
}

func assertAllDecrypt(t *testing.T, db *sql.DB, master []byte, seed map[string]string) {
	t.Helper()
	check := func(purpose, want string, token string) {
		got, err := DecryptWith(token, purpose, master)
		if err != nil || got != want {
			t.Fatalf("换钥后解密不一致 (%s): %v", purpose, err)
		}
	}
	check(PurposeSteamCookies, seed["cookiePlain"], dbScalar(t, db, `SELECT cookies FROM steam_accounts WHERE steam_id='sid-1'`))
	var apiKeyRaw, legacyRaw string
	if err := db.QueryRow(`SELECT value_json FROM app_settings WHERE key='account.steam_api_key'`).Scan(&apiKeyRaw); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow(`SELECT value_json FROM app_settings WHERE key='pilot.llm.provider_key.legacy-v1'`).Scan(&legacyRaw); err != nil {
		t.Fatal(err)
	}
	var apiKeyVal, legacyVal string
	if err := json.Unmarshal([]byte(apiKeyRaw), &apiKeyVal); err != nil {
		t.Fatalf("value_json 非 JSON 字符串: %q", apiKeyRaw)
	}
	if err := json.Unmarshal([]byte(legacyRaw), &legacyVal); err != nil {
		t.Fatalf("value_json 非 JSON 字符串: %q", legacyRaw)
	}
	check(SecretPurpose("account.steam_api_key"), seed["apiKeyPlain"], apiKeyVal)
	check(SecretPurpose("pilot.llm.provider_key.legacy-v1"), seed["legacyKeyPlain"], legacyVal)
	check(PurposeProxySubscription, seed["subURLPlain"], dbScalar(t, db, `SELECT url FROM proxy_subscriptions WHERE id=1`))
	check(PurposeProxySubscription, seed["snapURLPlain"], dbScalar(t, db, `SELECT url FROM subscription_snapshots WHERE id=1`))
	check(PurposeProxyCredential, seed["proxyUserPlain"], dbScalar(t, db, `SELECT username FROM proxies WHERE id=1`))
	check(PurposeProxyCredential, seed["proxyPassPlain"], dbScalar(t, db, `SELECT password FROM proxies WHERE id=1`))
}

func TestSwitchModeToPassphraseAndBack(t *testing.T) {
	ctx := context.Background()
	dir, gate, db := rekeyEnv(t)
	ResetState()
	t.Cleanup(ResetState)
	legacy, err := LegacyMaster(dir)
	if err != nil {
		t.Fatal(err)
	}
	seed := seedCiphertexts(t, db, legacy)

	res, err := SwitchMode(ctx, gate, dir, ModePassphrase, "new-pass", "")
	if err != nil {
		t.Fatal(err)
	}
	if res.Mode != ModePassphrase {
		t.Fatalf("切换结果模式漂移: %q", res.Mode)
	}
	want := Counts{AccountCookies: 1, SettingSecrets: 2, Subscriptions: 1, Snapshots: 1, ProxyCredentials: 2}
	if res.Counts != want {
		t.Fatalf("换钥条数漂移: %+v != %+v", res.Counts, want)
	}
	if MigrationPending(dir) {
		t.Fatal("切换成功后过渡记录应已删除")
	}
	mode, err := ModeOf(dir)
	if err != nil || mode != ModePassphrase {
		t.Fatalf("密钥文件模式漂移: %q %v", mode, err)
	}
	newMaster, err := ResolveDMK(dir)
	if err != nil {
		t.Fatal(err)
	}
	assertAllDecrypt(t, db, newMaster, seed)
	// 非密文 / 明文 / 空值行未被改写
	if got := dbScalar(t, db, `SELECT cookies FROM steam_accounts WHERE steam_id='sid-plain'`); got != "plaintext-cookie" {
		t.Fatalf("明文行被误改: %q", got)
	}
	if got := dbScalar(t, db, `SELECT value_json FROM app_settings WHERE key='games.derived_inputs'`); got != `{"series": {}}` {
		t.Fatalf("对象行被误改: %q", got)
	}
	if got := dbScalar(t, db, `SELECT url FROM proxy_subscriptions WHERE id=2`); got != "plain-url" {
		t.Fatalf("明文订阅行被误改: %q", got)
	}

	// 切回 legacy：密钥文件删除，全库回到机器派生主密钥
	res2, err := SwitchMode(ctx, gate, dir, ModeLegacy, "", "")
	if err != nil {
		t.Fatal(err)
	}
	if res2.Counts != want {
		t.Fatalf("切回条数漂移: %+v", res2.Counts)
	}
	mode, err = ModeOf(dir)
	if err != nil || mode != ModeLegacy {
		t.Fatalf("切回后应无密钥文件: %q %v", mode, err)
	}
	assertAllDecrypt(t, db, legacy, seed)
}

func TestSwitchModeValidation(t *testing.T) {
	ctx := context.Background()
	dir, gate, _ := rekeyEnv(t)
	if _, err := SwitchMode(ctx, gate, dir, "bogus", "", ""); err == nil || err.Error() != "未知的密钥模式：'bogus'" {
		t.Fatalf("未知模式文案漂移: %v", err)
	}
	if _, err := SwitchMode(ctx, gate, dir, ModePassphrase, "", ""); err == nil || err.Error() != "口令不能为空" {
		t.Fatalf("空口令文案漂移: %v", err)
	}
}

func TestSwitchModeFailsClosedOnUndecryptable(t *testing.T) {
	ctx := context.Background()
	dir, gate, db := rekeyEnv(t)
	ResetState()
	t.Cleanup(ResetState)
	// 一行用陌生主密钥封装：换钥必须整体失败并回滚，不留半迁移
	rogue := randBytes(keyBytes)
	bad := mustJSONString(mustEncrypt(t, "orphan", SecretPurpose("account.steam_api_key"), rogue))
	exec := func(q string, args ...any) {
		if _, err := db.Exec(q, args...); err != nil {
			t.Fatal(err)
		}
	}
	exec(`INSERT INTO app_settings (key, value_json) VALUES ('account.steam_api_key', ?)`, bad)

	if _, err := SwitchMode(ctx, gate, dir, ModePassphrase, "new-pass", ""); err == nil {
		t.Fatal("含解不开的密文时换钥应整体失败")
	}
	if MigrationPending(dir) {
		t.Fatal("失败后过渡记录应已清理")
	}
	mode, err := ModeOf(dir)
	if err != nil || mode != ModeLegacy {
		t.Fatalf("失败后密钥文件应回滚到 legacy: %q %v", mode, err)
	}
	if got := dbScalar(t, db, `SELECT value_json FROM app_settings WHERE key='account.steam_api_key'`); got != bad {
		t.Fatal("失败后库内密文被部分改写")
	}
}

func mustEncrypt(t *testing.T, plain, purpose string, master []byte) string {
	t.Helper()
	token, err := EncryptWith(plain, purpose, master)
	if err != nil {
		t.Fatal(err)
	}
	return token
}

func TestResumePendingMigration(t *testing.T) {
	ctx := context.Background()
	dir, gate, db := rekeyEnv(t)
	ResetState()
	t.Cleanup(ResetState)
	legacy, err := LegacyMaster(dir)
	if err != nil {
		t.Fatal(err)
	}
	seed := seedCiphertexts(t, db, legacy)

	// 模拟崩溃窗口：新钥文件与过渡记录已落，库未重加密
	newMaster := randBytes(keyBytes)
	rec, err := WrapPassphrase(newMaster, "crash-pass")
	if err != nil {
		t.Fatal(err)
	}
	if err := WriteRecord(dir, rec); err != nil {
		t.Fatal(err)
	}
	if err := WriteStaging(dir, WrapStaging(legacy, newMaster)); err != nil {
		t.Fatal(err)
	}
	if !MigrationPending(dir) {
		t.Fatal("应判待收尾")
	}

	res, err := ResumePendingMigration(ctx, gate, dir, "")
	if err != nil {
		t.Fatal(err)
	}
	if res.Resumed || res.Reason != "过渡记录需要口令解锁" {
		t.Fatalf("锁定态应保留记录等解锁: %+v", res)
	}

	LoadUnlockedKey(newMaster)
	res, err = ResumePendingMigration(ctx, gate, dir, "")
	if err != nil {
		t.Fatal(err)
	}
	if !res.Resumed {
		t.Fatalf("续跑未完成: %+v", res)
	}
	want := Counts{AccountCookies: 1, SettingSecrets: 2, Subscriptions: 1, Snapshots: 1, ProxyCredentials: 2}
	if res.Counts != want {
		t.Fatalf("续跑条数漂移: %+v != %+v", res.Counts, want)
	}
	if MigrationPending(dir) {
		t.Fatal("续跑后过渡记录应删除")
	}
	assertAllDecrypt(t, db, newMaster, seed)

	res, err = ResumePendingMigration(ctx, gate, dir, "")
	if err != nil {
		t.Fatal(err)
	}
	if res.Resumed || res.Reason != "无待收尾的换钥" {
		t.Fatalf("续跑应幂等: %+v", res)
	}
}

func TestResumeTolerantPartialMigration(t *testing.T) {
	ctx := context.Background()
	dir, gate, db := rekeyEnv(t)
	ResetState()
	t.Cleanup(ResetState)
	legacy, err := LegacyMaster(dir)
	if err != nil {
		t.Fatal(err)
	}
	newMaster := randBytes(keyBytes)
	rec, _ := WrapPassphrase(newMaster, "crash-pass")
	if err := WriteRecord(dir, rec); err != nil {
		t.Fatal(err)
	}
	if err := WriteStaging(dir, WrapStaging(legacy, newMaster)); err != nil {
		t.Fatal(err)
	}
	// 一半旧行、一半已换新行：续跑只迁旧的一半
	if _, err := db.Exec(`INSERT INTO steam_accounts (steam_id, cookies) VALUES ('sid-old', ?)`,
		mustEncrypt(t, "old-plain", PurposeSteamCookies, legacy)); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO steam_accounts (steam_id, cookies) VALUES ('sid-new', ?)`,
		mustEncrypt(t, "new-plain", PurposeSteamCookies, newMaster)); err != nil {
		t.Fatal(err)
	}
	LoadUnlockedKey(newMaster)
	res, err := ResumePendingMigration(ctx, gate, dir, "")
	if err != nil {
		t.Fatal(err)
	}
	if !res.Resumed || res.Counts.AccountCookies != 1 {
		t.Fatalf("容忍续跑条数漂移: %+v", res)
	}
	if got := DecryptIn(t, db, `SELECT cookies FROM steam_accounts WHERE steam_id='sid-new'`, PurposeSteamCookies, newMaster); got != "new-plain" {
		t.Fatalf("已换新行被二次改写: %q", got)
	}
	if got := DecryptIn(t, db, `SELECT cookies FROM steam_accounts WHERE steam_id='sid-old'`, PurposeSteamCookies, newMaster); got != "old-plain" {
		t.Fatalf("旧行未迁: %q", got)
	}
}

func TestResumeCrashBeforeFileSwap(t *testing.T) {
	ctx := context.Background()
	dir, gate, _ := rekeyEnv(t)
	ResetState()
	t.Cleanup(ResetState)
	legacy, err := LegacyMaster(dir)
	if err != nil {
		t.Fatal(err)
	}
	// 崩溃发生在写密钥文件之前：过渡记录封的旧钥 == 当前钥 → 只清记录
	if err := WriteStaging(dir, WrapStaging(legacy, legacy)); err != nil {
		t.Fatal(err)
	}
	res, err := ResumePendingMigration(ctx, gate, dir, "")
	if err != nil {
		t.Fatal(err)
	}
	if !res.Resumed || res.Counts != (Counts{}) {
		t.Fatalf("崩溃在写文件前应只清记录: %+v", res)
	}
	if MigrationPending(dir) {
		t.Fatal("记录应已删除")
	}
}

// DecryptIn 取库内密文并用指定主密钥解回（测试辅助）。
func DecryptIn(t *testing.T, db *sql.DB, q, purpose string, master []byte) string {
	t.Helper()
	var token string
	if err := db.QueryRow(q).Scan(&token); err != nil {
		t.Fatal(err)
	}
	if len(token) > 1 && token[0] == '"' && token[len(token)-1] == '"' {
		if err := json.Unmarshal([]byte(token), &token); err != nil {
			t.Fatal(err)
		}
	}
	got, err := DecryptWith(token, purpose, master)
	if err != nil {
		t.Fatal(err)
	}
	return got
}
