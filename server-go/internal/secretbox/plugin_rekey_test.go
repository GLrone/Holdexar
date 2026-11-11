package secretbox

import (
	"encoding/json"
	"testing"
)

func pluginRegistryFixture(t *testing.T, tokens map[string]string) string {
	t.Helper()
	config := map[string]any{"label": "keep me", "count": 9007199254740993, "nested": map[string]any{"enabled": true}}
	for field, token := range tokens {
		config[field] = token
	}
	raw, err := json.Marshal([]any{map[string]any{
		"manifest": map[string]any{"id": "example.webhook", "version": "1.2.3", "configSchema": map[string]any{"properties": map[string]any{"token": map[string]any{"type": "string", "format": "password"}}}},
		"config":   config, "enabled": true, "targetDigest": "unchanged-target", "futureField": map[string]any{"retained": "yes"},
	}})
	if err != nil {
		t.Fatal(err)
	}
	return string(raw)
}

func TestPluginSecretsFollowGlobalKeyRotationWithoutChangingOtherState(t *testing.T) {
	_, gate, db := rekeyEnv(t)
	old, next := randBytes(keyBytes), randBytes(keyBytes)
	raw := pluginRegistryFixture(t, map[string]string{
		"token": mustEncrypt(t, "secret", "plugin:example.webhook:token", old),
		"other": mustEncrypt(t, "second", "plugin:example.webhook:other", old),
	})
	if _, err := db.Exec(`INSERT INTO app_settings(key,value_json) VALUES('plugins.registry',?)`, raw); err != nil {
		t.Fatal(err)
	}
	counts, err := ReencryptAll(t.Context(), gate, old, next)
	if err != nil {
		t.Fatal(err)
	}
	if counts.SettingSecrets != 1 {
		t.Fatalf("updated settings rows=%d, want 1", counts.SettingSecrets)
	}
	var before, after []map[string]json.RawMessage
	if err := json.Unmarshal([]byte(raw), &before); err != nil {
		t.Fatal(err)
	}
	saved := dbScalar(t, db, `SELECT value_json FROM app_settings WHERE key='plugins.registry'`)
	if err := json.Unmarshal([]byte(saved), &after); err != nil {
		t.Fatal(err)
	}
	for key, value := range before[0] {
		if key != "config" && string(after[0][key]) != string(value) {
			t.Fatalf("changed %s: %s", key, after[0][key])
		}
	}
	var beforeConfig, afterConfig map[string]json.RawMessage
	json.Unmarshal(before[0]["config"], &beforeConfig)
	json.Unmarshal(after[0]["config"], &afterConfig)
	for field, value := range beforeConfig {
		if field != "token" && field != "other" {
			if string(afterConfig[field]) != string(value) {
				t.Fatalf("changed non-secret %s", field)
			}
			continue
		}
		var token string
		if err := json.Unmarshal(afterConfig[field], &token); err != nil {
			t.Fatal(err)
		}
		purpose := "plugin:example.webhook:" + field
		if plain, err := DecryptWith(token, purpose, next); err != nil || (plain != "secret" && plain != "second") {
			t.Fatalf("new key cannot read %s: %v", field, err)
		}
		if _, err := DecryptWith(token, purpose, old); err == nil {
			t.Fatal("old key still reads migrated secret")
		}
	}
}

func TestPluginRotationFailureRollsBackAllTables(t *testing.T) {
	for _, invalid := range []string{`{}`, `[null]`, `[{"manifest":{"id":"example.webhook"},"config":[]}]`, `[{"manifest":{},"config":{"token":"enc1:broken"}}]`, pluginRegistryFixture(t, map[string]string{"token": "enc1:broken"})} {
		t.Run(invalid, func(t *testing.T) {
			_, gate, db := rekeyEnv(t)
			old, next := randBytes(keyBytes), randBytes(keyBytes)
			seed := seedCiphertexts(t, db, old)
			if _, err := db.Exec(`INSERT INTO app_settings(key,value_json) VALUES('plugins.registry',?)`, invalid); err != nil {
				t.Fatal(err)
			}
			if _, err := ReencryptAll(t.Context(), gate, old, next); err == nil {
				t.Fatal("invalid plugin state did not abort rotation")
			}
			if got := dbScalar(t, db, `SELECT cookies FROM steam_accounts WHERE steam_id='sid-1'`); got != seed["cookie"] {
				t.Fatal("earlier table update escaped rollback")
			}
			if got := dbScalar(t, db, `SELECT value_json FROM app_settings WHERE key='plugins.registry'`); got != invalid {
				t.Fatal("invalid registry was overwritten")
			}
		})
	}
}

func TestPluginResumeKeepsNewOrUnreadableTokensAndMigratesOldTokens(t *testing.T) {
	_, gate, db := rekeyEnv(t)
	old, next := randBytes(keyBytes), randBytes(keyBytes)
	already := mustEncrypt(t, "already", "plugin:example.webhook:already", next)
	raw := pluginRegistryFixture(t, map[string]string{
		"token": mustEncrypt(t, "old", "plugin:example.webhook:token", old), "already": already, "unreadable": "enc1:broken",
	})
	if _, err := db.Exec(`INSERT INTO app_settings(key,value_json) VALUES('plugins.registry',?)`, raw); err != nil {
		t.Fatal(err)
	}
	counts, err := resumeReencrypt(t.Context(), gate, old, next)
	if err != nil || counts.SettingSecrets != 1 {
		t.Fatalf("resume: %+v %v", counts, err)
	}
	var entries []struct{ Config map[string]json.RawMessage }
	json.Unmarshal([]byte(dbScalar(t, db, `SELECT value_json FROM app_settings WHERE key='plugins.registry'`)), &entries)
	var migrated, kept, unreadable string
	json.Unmarshal(entries[0].Config["token"], &migrated)
	json.Unmarshal(entries[0].Config["already"], &kept)
	json.Unmarshal(entries[0].Config["unreadable"], &unreadable)
	if plain, err := DecryptWith(migrated, "plugin:example.webhook:token", next); err != nil || plain != "old" {
		t.Fatalf("old token not resumed: %v", err)
	}
	if kept != already || unreadable != "enc1:broken" {
		t.Fatal("tolerant resume altered skipped tokens")
	}
	counts, err = resumeReencrypt(t.Context(), gate, old, next)
	if err != nil || counts.SettingSecrets != 0 {
		t.Fatalf("resume not idempotent: %+v %v", counts, err)
	}
}
