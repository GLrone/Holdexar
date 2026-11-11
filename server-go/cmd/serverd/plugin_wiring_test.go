//go:build windows

package main

import (
	"context"
	"path/filepath"
	"testing"

	"holdexar/server-go/internal/epicfree"
	"holdexar/server-go/internal/pilot"
	"holdexar/server-go/internal/store"
)

func TestPilotEpicUsesModuleLifecycle(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, e := store.Open(path)
	if e != nil {
		t.Fatal(e)
	}
	defer db.Close()
	if e = store.InitializeFreshDatabase(ctx, db); e != nil {
		t.Fatal(e)
	}
	service := epicfree.New(ctx, db, store.NewWriteGate(db, path), epicfree.Options{})
	defer service.Close(ctx)
	if e = service.SetEnabled(ctx, false); e != nil {
		t.Fatal(e)
	}
	result := pilotEpicFree(&pilot.CallCtx{Ctx: ctx}, service)
	if result["kind"] != "disabled" {
		t.Fatalf("disabled module returned %+v", result)
	}
}

func TestPilotModuleLifecycle(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, e := store.Open(path)
	if e != nil {
		t.Fatal(e)
	}
	defer db.Close()
	if e = store.InitializeFreshDatabase(ctx, db); e != nil {
		t.Fatal(e)
	}
	gate := store.NewWriteGate(db, path)
	service := pilot.New(pilot.Deps{DB: db, DataDir: t.TempDir()}, func(*pilot.CallCtx, string, map[string]any, bool) map[string]any { return map[string]any{} })
	service.SetGate(gate)

	enabled, running, err := service.State(ctx)
	if err != nil || !enabled || running {
		t.Fatalf("initial state=%t/%t err=%v", enabled, running, err)
	}
	// 停用：无在飞轮立即收尾；状态翻转并持久。
	if e = service.SetEnabled(ctx, false); e != nil {
		t.Fatal(e)
	}
	enabled, running, err = service.State(ctx)
	if err != nil || enabled || running {
		t.Fatalf("disabled state=%t/%t err=%v", enabled, running, err)
	}
	// 发问入口拒新（用户语言拒因）。
	_, err = service.AskStream(ctx, pilot.AskRequest{Question: "你好"})
	if err == nil || err.Error() != pilot.ErrModuleDisabled.Error() {
		t.Fatalf("ask while disabled err=%v", err)
	}
	// 新服务实例从库读回停用态（跨重启语义）。
	reborn := pilot.New(pilot.Deps{DB: db, DataDir: t.TempDir()}, nil)
	reborn.SetGate(gate)
	if enabled, _, e = reborn.State(ctx); e != nil || enabled {
		t.Fatalf("reborn state=%t err=%v", enabled, e)
	}
	// 重开恢复。
	if e = service.SetEnabled(ctx, true); e != nil {
		t.Fatal(e)
	}
	if enabled, _, e = service.State(ctx); e != nil || !enabled {
		t.Fatalf("re-enabled state=%t err=%v", enabled, e)
	}
}
