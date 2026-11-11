package account

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"testing"

	"holdexar/server-go/internal/secretbox"
)

func seedLegacyAccount(t *testing.T, s *Service, cookie string) {
	t.Helper()
	for key, value := range map[string]any{
		legacyCookiesKey: cookie, "account.wallet_snapshot": map[string]any{"steam_id": "76561198000000001", "balance": 17.5, "ok_at": "2026-11-01T12:00:00"},
		"account.profile": map[string]any{"persona_name": "Legacy Name", "avatar_url": "legacy-avatar"},
	} {
		raw, _ := json.Marshal(value)
		if _, e := s.db.Exec(`INSERT INTO app_settings(key,value_json) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json`, key, string(raw)); e != nil {
			t.Fatal(e)
		}
	}
}

func TestLegacyAccountStartupMigrationIsEncryptedAtomicAndIdempotent(t *testing.T) {
	s, db := serviceFixture(t, func(w http.ResponseWriter, r *http.Request) {
		t.Error("startup must not fetch Steam")
		w.WriteHeader(500)
	})
	ctx := context.Background()
	cookie := "steamLoginSecure=76561198000000001||legacy-fixture"
	seedLegacyAccount(t, s, cookie)
	if n, e := s.MigrateLegacyKV(ctx); e != nil || n != 1 {
		t.Fatalf("migrated=%d error=%v", n, e)
	}
	status, e := s.Status(ctx)
	if e != nil || status["cookie_steam_id"] != "76561198000000001" || status["wallet"].(map[string]any)["balance"] != 17.5 || status["profile"].(map[string]any)["persona_name"] != "Legacy Name" {
		t.Fatalf("status=%v error=%v", status, e)
	}
	var token string
	if e = db.QueryRow(`SELECT cookies FROM steam_accounts`).Scan(&token); e != nil || !strings.HasPrefix(token, secretbox.Prefix) {
		t.Fatalf("credential=%s error=%v", token, e)
	}
	if plain, e := secretbox.DecryptSecretIn(s.opts.DataDir, token, credentialPurpose); e != nil || plain != cookie {
		t.Fatalf("credential round trip failed: %v", e)
	}
	var cleared int
	if e = db.QueryRow(`SELECT count(*) FROM app_settings WHERE key IN('account.steam_cookies','account.wallet_snapshot','account.profile') AND value_json IN('""','null')`).Scan(&cleared); e != nil || cleared != 3 {
		t.Fatalf("legacy cleared=%d %v", cleared, e)
	}
	if n, e := s.MigrateLegacyKV(ctx); e != nil || n != 0 {
		t.Fatalf("second startup migrated=%d error=%v", n, e)
	}
}

func TestLegacyAccountMigrationFailureKeepsOriginalCredentials(t *testing.T) {
	s, db := serviceFixture(t, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(500) })
	seedLegacyAccount(t, s, "steamLoginSecure=76561198000000001||legacy-fixture")
	if _, e := db.Exec(`CREATE TRIGGER reject_legacy_clear BEFORE UPDATE ON app_settings WHEN OLD.key='account.steam_cookies' BEGIN SELECT RAISE(ABORT,'controlled failure'); END`); e != nil {
		t.Fatal(e)
	}
	if _, e := s.MigrateLegacyKV(context.Background()); e == nil {
		t.Fatal("migration failure swallowed")
	}
	var count int
	if e := db.QueryRow(`SELECT count(*) FROM steam_accounts`).Scan(&count); e != nil || count != 0 {
		t.Fatalf("partial migrated row=%d %v", count, e)
	}
	var original string
	db.QueryRow(`SELECT value_json FROM app_settings WHERE key='account.steam_cookies'`).Scan(&original)
	if !strings.Contains(original, "legacy-fixture") {
		t.Fatal("original credential was lost")
	}
}

func TestStartupSealPreservesExistingCiphertextAndAccountOwnership(t *testing.T) {
	s, db := serviceFixture(t, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(429) })
	ctx := context.Background()
	if _, e := s.BindCookies(ctx, "steamLoginSecure=76561198000000002||new-fixture"); e != nil {
		t.Fatal(e)
	}
	seedLegacyAccount(t, s, "steamLoginSecure=76561198000000001||legacy-fixture")
	if n, e := s.MigrateLegacyKV(ctx); e != nil || n != 1 {
		t.Fatalf("%d %v", n, e)
	}
	status, e := s.Status(ctx)
	if e != nil || status["cookie_steam_id"] != "76561198000000002" {
		t.Fatalf("startup stole active identity: %v %v", status, e)
	}
	var before string
	db.QueryRow(`SELECT cookies FROM steam_accounts WHERE steam_id='76561198000000002'`).Scan(&before)
	if _, e = db.Exec(`UPDATE steam_accounts SET cookies='steamLoginSecure=76561198000000001||plain-fixture' WHERE steam_id='76561198000000001'`); e != nil {
		t.Fatal(e)
	}
	if n, e := s.SealCredentials(ctx); e != nil || n != 1 {
		t.Fatalf("sealed=%d error=%v", n, e)
	}
	var after string
	db.QueryRow(`SELECT cookies FROM steam_accounts WHERE steam_id='76561198000000002'`).Scan(&after)
	if after != before {
		t.Fatal("existing ciphertext rewritten")
	}
	if n, e := s.SealCredentials(ctx); e != nil || n != 0 {
		t.Fatalf("second sealing=%d error=%v", n, e)
	}
	seedLegacyAccount(t, s, "steamLoginSecure=76561198000000002||stale-kv")
	if n, e := s.MigrateLegacyKV(ctx); e != nil || n != 0 {
		t.Fatalf("existing account migration=%d error=%v", n, e)
	}
	db.QueryRow(`SELECT cookies FROM steam_accounts WHERE steam_id='76561198000000002'`).Scan(&after)
	if after != before {
		t.Fatal("stale legacy credential replaced current account")
	}
}

func TestLegacyMigrationDoesNotAssignAnotherAccountsWallet(t *testing.T) {
	s, db := serviceFixture(t, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(500) })
	seedLegacyAccount(t, s, "steamLoginSecure=76561198000000001||legacy-fixture")
	if _, e := db.Exec(`UPDATE app_settings SET value_json='{"steam_id":"76561198000000002","balance":99}' WHERE key='account.wallet_snapshot'`); e != nil {
		t.Fatal(e)
	}
	if n, e := s.MigrateLegacyKV(context.Background()); e == nil || n != 0 {
		t.Fatalf("wrong-owner snapshot was accepted: n=%d error=%v", n, e)
	}
	var count int
	if e := db.QueryRow(`SELECT count(*) FROM steam_accounts`).Scan(&count); e != nil || count != 0 {
		t.Fatalf("partial migration=%d %v", count, e)
	}
}
