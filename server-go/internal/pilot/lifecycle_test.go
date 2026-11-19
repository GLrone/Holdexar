package pilot

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
)

func TestSetEnabledWaitsForInflightTurns(t *testing.T) {
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
	service := New(Deps{DB: db, DataDir: t.TempDir()}, nil)
	service.SetGate(store.NewWriteGate(db, path))

	accepted, _ := service.acquireTurn("s1")
	if !accepted {
		t.Fatal("turn not accepted")
	}
	done := make(chan error, 1)
	go func() { done <- service.SetEnabled(ctx, false) }()

	select {
	case err := <-done:
		t.Fatalf("SetEnabled returned before inflight finished: %v", err)
	case <-time.After(300 * time.Millisecond):
	}
	if _, running, _ := service.State(ctx); !running {
		t.Fatal("state.running must be true while turn in flight")
	}
	service.releaseTurn("s1")
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("SetEnabled after release: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("SetEnabled did not finish after inflight release")
	}
	if enabled, running, _ := service.State(ctx); enabled || running {
		t.Fatalf("state after disable=%t/%t", enabled, running)
	}
	if _, err := service.AskStream(ctx, AskRequest{Question: "你好"}); err != ErrModuleDisabled {
		t.Fatalf("ask while disabled err=%v", err)
	}
}
