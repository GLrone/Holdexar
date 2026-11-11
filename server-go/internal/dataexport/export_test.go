package dataexport

import (
	"context"
	"crypto/aes"
	"crypto/cipher"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/crypto/scrypt"

	"holdexar/server-go/internal/secretbox"
	"holdexar/server-go/internal/store"
)

func exportFixture(t *testing.T) (*sql.DB, string) {
	t.Helper()
	dir := t.TempDir()
	db, err := store.Open(filepath.Join(dir, "holdexar.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err := store.InitializeFreshDatabase(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	cookie, err := secretbox.EncryptSecretIn(dir, "sessionid=real-cookie", secretbox.PurposeSteamCookies)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO steam_accounts(steam_id, cookies, persona_name, avatar_url, is_active, is_online, in_game, wallet_error, wallet_backoff_level, wallet_fail_streak, wallet_frozen) VALUES('765', ?, '测试', '', 1, 0, '', '', 0, 0, 0)`, cookie); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO app_settings(key, value_json, updated_at) VALUES('seed.marker', '"v1"', '2026-01-01'),
		('ui.theme', '"dark"', '2026-01-02')`); err != nil {
		t.Fatal(err)
	}
	return db, dir
}

func TestExportCreateEncryptsAndSkipsSeedKeys(t *testing.T) {
	db, dir := exportFixture(t)
	result, err := Create(context.Background(), db, dir, "a-long-enough-passphrase")
	if err != nil {
		t.Fatal(err)
	}
	if result.SizeBytes <= 0 || !strings.HasSuffix(result.Name, exportSuffix) {
		t.Fatalf("result=%+v", result)
	}
	raw, err := os.ReadFile(result.Path)
	if err != nil {
		t.Fatal(err)
	}
	var envelope struct {
		KDF struct {
			Salt string `json:"salt"`
		} `json:"kdf"`
		Nonce      string `json:"nonce"`
		Ciphertext string `json:"ciphertext"`
	}
	if err := json.Unmarshal(raw, &envelope); err != nil {
		t.Fatal(err)
	}
	salt, _ := base64.RawURLEncoding.DecodeString(envelope.KDF.Salt)
	nonce, _ := base64.RawURLEncoding.DecodeString(envelope.Nonce)
	sealed, _ := base64.RawURLEncoding.DecodeString(envelope.Ciphertext)
	kek, err := scrypt.Key([]byte("a-long-enough-passphrase"), salt, scryptN, scryptR, scryptP, scryptKeyLen)
	if err != nil {
		t.Fatal(err)
	}
	block, _ := aes.NewCipher(kek)
	gcm, _ := cipher.NewGCM(block)
	plain, err := gcm.Open(nil, nonce, sealed, nil)
	if err != nil {
		t.Fatalf("正确口令应能解开信封: %v", err)
	}
	text := string(plain)
	if !strings.Contains(text, "sessionid=real-cookie") {
		t.Fatal("凭据应解密后随正文导出")
	}
	if strings.Contains(text, "seed.marker") {
		t.Fatal("种子 marker 不得进导出")
	}
	if strings.Contains(text, `"dark"`) == false {
		t.Fatal("普通设置应随正文导出")
	}
}

func TestExportShortPasswordRejected(t *testing.T) {
	db, dir := exportFixture(t)
	if _, err := Create(context.Background(), db, dir, "short"); err == nil ||
		!strings.Contains(err.Error(), "12") {
		t.Fatalf("过短口令必须被拒：%v", err)
	}
}

func TestSafeExportPathRejectsTraversal(t *testing.T) {
	dir := t.TempDir()
	for _, name := range []string{"../x.hxexport", `sub\x.hxexport`, "x.txt", "", "a..b.hxexport"} {
		if _, err := SafeExportPath(dir, name); err == nil {
			t.Fatalf("%q 必须被拒", name)
		}
	}
	if _, err := SafeExportPath(dir, "holdexar-secure-1.hxexport"); err != nil {
		t.Fatalf("合法名被误拒：%v", err)
	}
}
