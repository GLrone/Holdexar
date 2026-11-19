package main

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"holdexar/server-go/internal/access"
	"holdexar/server-go/internal/secretbox"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
)

func lanTestDB(t *testing.T, fresh bool) (*sql.DB, *store.WriteGate, string, context.CancelFunc) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	dataDir := t.TempDir()
	db, err := store.Open(filepath.Join(dataDir, "holdexar.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if fresh {
		if err := store.InitializeFreshDatabase(ctx, db); err != nil {
			t.Fatal(err)
		}
	}
	return db, store.NewWriteGate(db, filepath.Join(dataDir, "gate.lock")), dataDir, cancel
}

func randTestSuffix(t *testing.T, n int) string {
	t.Helper()
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		t.Fatal(err)
	}
	return hex.EncodeToString(b)
}

func TestResolveLANPasswordVerifierMigratesLegacy(t *testing.T) {
	db, gate, dataDir, cancel := lanTestDB(t, true)
	defer cancel()
	ctx := context.Background()

	legacy := "legacy-" + randTestSuffix(t, 6)
	sealed, err := secretbox.EncryptSecretIn(dataDir, legacy, secretbox.SecretPurpose(settings.KeyLANPassword))
	if err != nil {
		t.Fatal(err)
	}
	if err := gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		return settings.WriteValue(ctx, conn, settings.KeyLANPassword, sealed)
	}); err != nil {
		t.Fatal(err)
	}

	verifier, err := resolveLANPasswordVerifier(ctx, db, gate, dataDir)
	if err != nil {
		t.Fatalf("验证器解析失败：%v", err)
	}
	if !strings.HasPrefix(verifier, "$scrypt$") || !access.VerifyPassword(legacy, verifier) {
		t.Fatalf("迁移后验证器应校验旧口令通过：%q", verifier)
	}
	var n int
	if err := db.QueryRow(`SELECT COUNT(*) FROM app_settings WHERE key = ?`, settings.KeyLANPassword).Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != 0 {
		t.Fatalf("旧密文键应清账，实际 %d 行", n)
	}
	again, err := resolveLANPasswordVerifier(ctx, db, gate, dataDir)
	if err != nil || again != verifier {
		t.Fatalf("迁移后应幂等直读验证器 KV：%q vs %q, %v", again, verifier, err)
	}
}

func TestResolveLANPasswordVerifierUserSettingWinsOverEnv(t *testing.T) {
	db, gate, dataDir, cancel := lanTestDB(t, true)
	defer cancel()
	ctx := context.Background()
	stored := "stored-" + randTestSuffix(t, 6)
	if err := gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		return settings.WriteValue(ctx, conn, settings.KeyLANPasswordHash, access.HashPassword(stored))
	}); err != nil {
		t.Fatal(err)
	}
	envPw := "env-" + randTestSuffix(t, 6)
	t.Setenv(access.EnvAccessPassword, envPw)
	verifier, err := resolveLANPasswordVerifier(ctx, db, gate, dataDir)
	if err != nil || !access.VerifyPassword(stored, verifier) {
		t.Fatalf("用户设置口令应优先：%q, %v", verifier, err)
	}
	if access.VerifyPassword(envPw, verifier) {
		t.Fatalf("环境口令不应被接受：%q", verifier)
	}
}

func TestResolveLANPasswordVerifierEnvFallback(t *testing.T) {
	db, gate, dataDir, cancel := lanTestDB(t, true)
	defer cancel()
	ctx := context.Background()
	envPw := "env-" + randTestSuffix(t, 6)
	t.Setenv(access.EnvAccessPassword, envPw)
	verifier, err := resolveLANPasswordVerifier(ctx, db, gate, dataDir)
	if err != nil || !access.VerifyPassword(envPw, verifier) {
		t.Fatalf("环境口令应生效：%q, %v", verifier, err)
	}
}

func TestResolveLANPasswordVerifierEmptyWhenUnset(t *testing.T) {
	db, gate, dataDir, cancel := lanTestDB(t, true)
	defer cancel()
	verifier, err := resolveLANPasswordVerifier(context.Background(), db, gate, dataDir)
	if err != nil || verifier != "" {
		t.Fatalf("未设密码应返回空串：%q, %v", verifier, err)
	}
}

func TestInitLANAccessEnables(t *testing.T) {
	db, gate, dataDir, cancel := lanTestDB(t, true)
	defer cancel()
	ctx := context.Background()
	t.Cleanup(func() {
		access.SetLANEnabled(false)
		access.SetExtraHosts(nil)
	})
	if _, err := db.Exec(
		`INSERT INTO app_settings(key,value_json) VALUES('access.lan_enabled','true')`); err != nil {
		t.Fatal(err)
	}
	initLANAccess(ctx, db, gate, dataDir, func(string, ...any) {}, func(string, ...any) {})
	if !access.LANEnabled() {
		t.Fatal("KV 开启后应生效")
	}
	if !access.HostAllowed("127.0.0.1:28765") {
		t.Fatal("回环放行不得受影响")
	}
}

func TestInitLANAccessFailClosed(t *testing.T) {
	db, gate, dataDir, cancel := lanTestDB(t, false)
	defer cancel()
	ctx := context.Background()
	access.SetLANEnabled(true)
	access.SetExtraHosts([]string{"192.168.1.5"})
	initLANAccess(ctx, db, gate, dataDir, func(string, ...any) {}, func(string, ...any) {})
	if access.LANEnabled() {
		t.Fatal("初始化失败应按关闭处理")
	}
	if access.HostAllowed("192.168.1.5") {
		t.Fatal("失败后白名单应回落回环-only")
	}
	if !access.HostAllowed("127.0.0.1") {
		t.Fatal("回环放行不得受影响")
	}
}
