package main

import (
	"context"
	"database/sql"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"holdexar/server-go/internal/access"
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

// 访问密码生成一次后稳定复用（落库密文），环境变量通道优先。
func TestEnsureLANPasswordGeneratesOnce(t *testing.T) {
	db, gate, dataDir, cancel := lanTestDB(t, true)
	defer cancel()
	ctx := context.Background()

	first, err := ensureLANPassword(ctx, db, gate, dataDir)
	if err != nil {
		t.Fatalf("密码生成失败：%v", err)
	}
	second, err := ensureLANPassword(ctx, db, gate, dataDir)
	if err != nil {
		t.Fatalf("密码复取失败：%v", err)
	}
	if first != second || len(first) < 8 {
		t.Fatalf("密码应生成一次后稳定复用：%q vs %q", first, second)
	}
	var stored string
	if err := db.QueryRow(
		`SELECT value_json FROM app_settings WHERE key='access.lan_password'`).Scan(&stored); err != nil {
		t.Fatalf("密码未落库：%v", err)
	}
	if !strings.HasPrefix(stored, `"enc1:`) {
		t.Fatalf("库内应为密文，实际 %s", stored)
	}
	t.Setenv(access.EnvAccessPassword, "env-lan-pw")
	if envPw, err := ensureLANPassword(ctx, db, gate, dataDir); err != nil || envPw != "env-lan-pw" {
		t.Fatalf("环境变量通道应优先：%q, %v", envPw, err)
	}
}

// initLANAccess 正向：KV 开启 + 密码落库 + 白名单刷新。
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

// initLANAccess 失败按关闭处理（fail-closed）：库不完整时开关回落关闭。
func TestInitLANAccessFailClosed(t *testing.T) {
	db, gate, dataDir, cancel := lanTestDB(t, false) // 无 schema：读 KV 必失败
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
