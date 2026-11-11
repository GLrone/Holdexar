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
