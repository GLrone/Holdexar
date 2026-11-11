package dataexport

import (
	"crypto/aes"
	"crypto/cipher"
	"encoding/base64"
	"encoding/json"
	"os"
	"strings"
	"testing"

	"golang.org/x/crypto/scrypt"
	"holdexar/server-go/internal/secretbox"
)

func TestPluginExportRemovesNonportableSecretsWithoutChangingSource(t *testing.T) {
	db, dir := exportFixture(t)
	token, err := secretbox.EncryptSecretIn(dir, "plugin-private-token", "plugin:example.webhook:token")
	if err != nil {
		t.Fatal(err)
	}
	entries := []any{map[string]any{
		"manifest": map[string]any{"id": "example.webhook", "version": "1.2.3", "configSchema": map[string]any{"properties": map[string]any{"token": map[string]any{"type": "string", "format": "password"}, "plainSecret": map[string]any{"type": "string", "format": "password"}}}},
		"enabled":  true, "configuredSecrets": []string{"token"}, "targetDigest": "old-target",
		"config": map[string]any{"token": token, "plainSecret": "legacy-plaintext-secret", "untypedSecret": "enc1:unreadable", "label": "preserve label", "count": 9},
	}}
	raw, _ := json.Marshal(entries)
	if _, err := db.Exec(`INSERT INTO app_settings(key,value_json) VALUES('plugins.registry',?)`, string(raw)); err != nil {
		t.Fatal(err)
	}
	result, err := Create(t.Context(), db, dir, "a-long-enough-passphrase")
	if err != nil {
		t.Fatal(err)
	}
	file, err := os.ReadFile(result.Path)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(file), "plugin-private-token") || strings.Contains(string(file), "legacy-plaintext-secret") || strings.Contains(string(file), token) {
		t.Fatal("export leaked plugin credential")
	}
	var envelope envelope
	if err := json.Unmarshal(file, &envelope); err != nil {
		t.Fatal(err)
	}
	salt, _ := base64.RawURLEncoding.DecodeString(envelope.KDF.Salt)
	nonce, _ := base64.RawURLEncoding.DecodeString(envelope.Nonce)
	encrypted, _ := base64.RawURLEncoding.DecodeString(envelope.Ciphertext)
	key, err := scrypt.Key([]byte("a-long-enough-passphrase"), salt, scryptN, scryptR, scryptP, scryptKeyLen)
	if err != nil {
		t.Fatal(err)
	}
	block, _ := aes.NewCipher(key)
	gcm, _ := cipher.NewGCM(block)
	plain, err := gcm.Open(nil, nonce, encrypted, nil)
	if err != nil {
		t.Fatal(err)
	}
	for _, secret := range []string{token, "plugin-private-token", "legacy-plaintext-secret", "enc1:unreadable"} {
		if strings.Contains(string(plain), secret) {
			t.Fatal("nonportable credential retained inside export")
		}
	}
	var body payload
	if err := json.Unmarshal(plain, &body); err != nil {
		t.Fatal(err)
	}
	var entry map[string]any
	for _, setting := range body.Tables["app_settings"] {
		if setting["key"] == "plugins.registry" {
			entry = setting["value"].([]any)[0].(map[string]any)
		}
	}
	if entry == nil || entry["enabled"] != false || entry["configured"] != false || entry["targetDigest"] != "" {
		t.Fatal("export still declares plugin ready")
	}
	config := entry["config"].(map[string]any)
	if len(config) != 2 || config["label"] != "preserve label" || config["count"] != float64(9) {
		t.Fatal("non-secret plugin configuration lost")
	}
	if _, ok := entry["configuredSecrets"]; ok {
		t.Fatal("export retains configured-secret indicator")
	}
	if entry["manifest"].(map[string]any)["version"] != "1.2.3" {
		t.Fatal("manifest lost")
	}
	if !strings.Contains(strings.Join(body.Excludes, " "), "插件凭据") {
		t.Fatal("export omission not disclosed")
	}
	var unchanged string
	if err := db.QueryRow(`SELECT value_json FROM app_settings WHERE key='plugins.registry'`).Scan(&unchanged); err != nil || unchanged != string(raw) {
		t.Fatal("export modified original plugin state", err)
	}
}

func TestPluginExportRejectsMalformedRegistry(t *testing.T) {
	for _, raw := range []string{`{}`, `[null]`, `[{"manifest":{"id":"example"},"config":[]}]`} {
		t.Run(raw, func(t *testing.T) {
			db, dir := exportFixture(t)
			if _, err := db.Exec(`INSERT INTO app_settings(key,value_json) VALUES('plugins.registry',?)`, raw); err != nil {
				t.Fatal(err)
			}
			if _, err := buildPayload(t.Context(), db, dir); err == nil {
				t.Fatal("malformed plugin registry silently exported")
			}
			if files := List(dir); len(files) != 0 {
				t.Fatal("failed export left an artifact")
			}
		})
	}
}
