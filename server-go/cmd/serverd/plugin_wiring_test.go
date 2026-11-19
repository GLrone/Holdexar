package main

import (
	"context"
	"net/http"
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
	gate := store.NewWriteGate(db, path)
	service := epicfree.New(ctx, db, gate, epicfree.Options{})
	defer service.Close(ctx)
	mux := http.NewServeMux()
	service.Register(mux)
	if e = service.SetEnabled(ctx, false); e != nil {
		t.Fatal(e)
	}
	for _, key := range []string{"module.humble_choice.enabled", "module.achievements.enabled"} {
		if _, err := db.ExecContext(ctx,
			`INSERT INTO app_settings (key, value_json, updated_at) VALUES (?, 'false', datetime('now'))
			 ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json`, key); err != nil {
			t.Fatal(err)
		}
	}
	bridge := pilotToolBridge(db, gate, mux, nil)
	callCtx := &pilot.CallCtx{Ctx: ctx, DB: db, Mux: mux}
	for _, tool := range []string{"epic_free", "hb_monthly", "achievements_summary", "sync_achievements"} {
		result := bridge(callCtx, tool, nil, false)
		if result["kind"] != "disabled" {
			t.Fatalf("%s disabled module returned %+v", tool, result)
		}
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
	if e = service.SetEnabled(ctx, false); e != nil {
		t.Fatal(e)
	}
	enabled, running, err = service.State(ctx)
	if err != nil || enabled || running {
		t.Fatalf("disabled state=%t/%t err=%v", enabled, running, err)
	}
	_, err = service.AskStream(ctx, pilot.AskRequest{Question: "你好"})
	if err == nil || err.Error() != pilot.ErrModuleDisabled.Error() {
		t.Fatalf("ask while disabled err=%v", err)
	}
	reborn := pilot.New(pilot.Deps{DB: db, DataDir: t.TempDir()}, nil)
	reborn.SetGate(gate)
	if enabled, _, e = reborn.State(ctx); e != nil || enabled {
		t.Fatalf("reborn state=%t err=%v", enabled, e)
	}
	if e = service.SetEnabled(ctx, true); e != nil {
		t.Fatal(e)
	}
	if enabled, _, e = service.State(ctx); e != nil || !enabled {
		t.Fatalf("re-enabled state=%t err=%v", enabled, e)
	}
}
