package settings

import (
	"context"
	"encoding/json"
	"path/filepath"
	"testing"

	"holdexar/server-go/internal/secretbox"
	"holdexar/server-go/internal/store"
)

func TestStartupSecretSealingKeepsPurposesCiphertextAndIdempotency(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "holdexar.db")
	db, e := store.Open(path)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { db.Close() })
	if _, e = db.Exec(`CREATE TABLE app_settings(key TEXT PRIMARY KEY,value_json TEXT,updated_at TEXT)`); e != nil {
		t.Fatal(e)
	}
	gate := store.NewWriteGate(db, path)
	cipher, e := secretbox.EncryptSecretIn(dir, "current-key", secretbox.SecretPurpose(keySteamAPIKey))
	if e != nil {
		t.Fatal(e)
	}
	for key, value := range map[string]string{keySteamAPIKey: cipher, "pilot.llm.api_key": "new-key", "pilot.llm.provider_key.legacy-v1": "old-key", "account.steam_id": "keep-id"} {
		raw, _ := json.Marshal(value)
		seedKV(t, db, key, string(raw))
	}
	if n, e := SealSecrets(context.Background(), db, gate, dir); e != nil || n != 2 {
		t.Fatalf("sealed=%d error=%v", n, e)
	}
	for key, expected := range map[string]string{keySteamAPIKey: "current-key", "pilot.llm.api_key": "new-key", "pilot.llm.provider_key.legacy-v1": "old-key"} {
		var raw, token string
		if e = db.QueryRow(`SELECT value_json FROM app_settings WHERE key=?`, key).Scan(&raw); e != nil {
			t.Fatal(e)
		}
		if e = json.Unmarshal([]byte(raw), &token); e != nil {
			t.Fatal(e)
		}
		if key == keySteamAPIKey && token != cipher {
			t.Fatal("existing ciphertext was rewritten")
		}
		if actual, e := secretbox.DecryptSecretIn(dir, token, secretbox.SecretPurpose(key)); e != nil || actual != expected {
			t.Fatalf("wrong credential purpose key=%s error=%v", key, e)
		}
	}
	if n, e := SealSecrets(context.Background(), db, gate, dir); e != nil || n != 0 {
		t.Fatalf("second seal=%d error=%v", n, e)
	}
	var identity string
	if e = db.QueryRow(`SELECT value_json FROM app_settings WHERE key='account.steam_id'`).Scan(&identity); e != nil || identity != `"keep-id"` {
		t.Fatalf("noncredential setting changed %v", e)
	}
	if _, e = db.Exec(`UPDATE app_settings SET value_json='"original-fixture"' WHERE key!='account.steam_id'; CREATE TRIGGER reject_secret BEFORE UPDATE ON app_settings WHEN OLD.key='pilot.llm.api_key' BEGIN SELECT RAISE(ABORT,'controlled failure'); END`); e != nil {
		t.Fatal(e)
	}
	if _, e = SealSecrets(context.Background(), db, gate, dir); e == nil {
		t.Fatal("sealing failure swallowed")
	}
	var unchanged int
	if e = db.QueryRow(`SELECT count(*) FROM app_settings WHERE value_json='"original-fixture"'`).Scan(&unchanged); e != nil || unchanged != 3 {
		t.Fatalf("partial secret sealing=%d error=%v", unchanged, e)
	}
}
