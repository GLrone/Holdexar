package main

import (
	"context"
	"database/sql"
	"errors"
	"path/filepath"
	"testing"
	"time"

	"holdexar/server-go/internal/events"
	"holdexar/server-go/internal/store"
)

func newRevertDB(t *testing.T) (*sql.DB, *store.WriteGate, *events.Bus, context.CancelFunc) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	db, err := store.Open(filepath.Join(t.TempDir(), "holdexar.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err := store.InitializeFreshDatabase(ctx, db); err != nil {
		t.Fatal(err)
	}
	gate := store.NewWriteGate(db, filepath.Join(t.TempDir(), "gate.lock"))
	return db, gate, events.NewBus(), cancel
}

func seedSetting(t *testing.T, db *sql.DB, key, value string) {
	t.Helper()
	if _, err := db.Exec(`INSERT INTO app_settings(key,value_json) VALUES(?,?)`, key, value); err != nil {
		t.Fatalf("种子设置失败: %v", err)
	}
}

func strategyValue(t *testing.T, db *sql.DB) string {
	t.Helper()
	var raw sql.NullString
	if err := db.QueryRow(`SELECT value_json FROM app_settings WHERE key='proxy.strategy'`).Scan(&raw); err != nil && !errors.Is(err, sql.ErrNoRows) {
		t.Fatalf("读取策略失败: %v", err)
	}
	return raw.String
}

func TestRevertGuardExplicit(t *testing.T) {
	db, gate, bus, cancel := newRevertDB(t)
	defer cancel()
	seedSetting(t, db, "proxy.strategy", `"proxy_first"`)
	seedSetting(t, db, "proxy.strategy_explicit", `true`)
	if maybeRevertToDirect(context.Background(), db, gate, bus, func(string, ...any) {}) {
		t.Fatal("用户显式选过策略，不得回退")
	}
	if got := strategyValue(t, db); got != `"proxy_first"` {
		t.Fatalf("策略不得改变，实际 %s", got)
	}
}

func TestRevertGuardNonDependentStrategy(t *testing.T) {
	db, gate, bus, cancel := newRevertDB(t)
	defer cancel()
	seedSetting(t, db, "proxy.strategy", `"direct_only"`)
	if maybeRevertToDirect(context.Background(), db, gate, bus, func(string, ...any) {}) {
		t.Fatal("非依赖代理池的策略，不得回退")
	}
}

func TestRevertGuardHasSubscription(t *testing.T) {
	db, gate, bus, cancel := newRevertDB(t)
	defer cancel()
	seedSetting(t, db, "proxy.strategy", `"proxy_first"`)
	if _, err := db.Exec(`INSERT INTO proxy_subscriptions(id,kind,url,deprecated) VALUES(1,'clash','https://example.invalid/sub',0)`); err != nil {
		t.Fatalf("订阅种子失败: %v", err)
	}
	if maybeRevertToDirect(context.Background(), db, gate, bus, func(string, ...any) {}) {
		t.Fatal("仍有未废弃订阅，不得回退")
	}
	if got := strategyValue(t, db); got != `"proxy_first"` {
		t.Fatalf("策略不得改变，实际 %s", got)
	}
}

func TestRevertGuardHasEnabledPool(t *testing.T) {
	db, gate, bus, cancel := newRevertDB(t)
	defer cancel()
	seedSetting(t, db, "proxy.strategy", `"proxy_first"`)
	if _, err := db.Exec(`INSERT INTO proxies(id,label,scheme,host,port,enabled,status,consecutive_failures) VALUES(1,'n','http','127.0.0.1',8888,1,'unknown',0)`); err != nil {
		t.Fatalf("节点种子失败: %v", err)
	}
	if maybeRevertToDirect(context.Background(), db, gate, bus, func(string, ...any) {}) {
		t.Fatal("手动池仍有启用节点，不得回退")
	}
	if got := strategyValue(t, db); got != `"proxy_first"` {
		t.Fatalf("策略不得改变，实际 %s", got)
	}
}

func TestRevertRevertsWhenNoExits(t *testing.T) {
	db, gate, bus, cancel := newRevertDB(t)
	defer cancel()
	seedSetting(t, db, "proxy.strategy", `"proxy_first"`)
	// 已废弃订阅不算可用出口
	if _, err := db.Exec(`INSERT INTO proxy_subscriptions(id,kind,url,deprecated) VALUES(1,'clash','https://example.invalid/sub',1)`); err != nil {
		t.Fatalf("订阅种子失败: %v", err)
	}
	if _, err := db.Exec(`INSERT INTO proxies(id,label,scheme,host,port,enabled,status,consecutive_failures) VALUES(1,'n','http','127.0.0.1',8888,0,'unknown',0)`); err != nil {
		t.Fatalf("节点种子失败: %v", err)
	}
	sub := bus.Subscribe()
	defer bus.Unsubscribe(sub)
	var logged []string
	if !maybeRevertToDirect(context.Background(), db, gate, bus, func(format string, args ...any) {
		logged = append(logged, format)
	}) {
		t.Fatal("无任何可用出口，应回退直连")
	}
	if got := strategyValue(t, db); got != `"direct_only"` {
		t.Fatalf("策略应已回退 direct_only，实际 %s", got)
	}
	if len(logged) == 0 {
		t.Fatal("回退应有日志")
	}
	ev, err := sub.Get(context.Background())
	if err != nil {
		t.Fatalf("settings.changed 未发布: %v", err)
	}
	if ev.Type != "settings.changed" {
		t.Fatalf("应发布 settings.changed，实际 %s", ev.Type)
	}
}
