package modulectl

import (
	"context"
	"path/filepath"
	"testing"

	"holdexar/server-go/internal/store"
)

func TestEnabledDefaultsTrueAndPersistsAcrossControls(t *testing.T) {
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

	ctl := New("module.test.enabled")
	enabled, e := ctl.Enabled(ctx, db)
	if e != nil || !enabled {
		t.Fatalf("default=%t err=%v", enabled, e)
	}
	if e = ctl.SetEnabled(ctx, db, gate, false); e != nil {
		t.Fatal(e)
	}
	if enabled, e = ctl.Enabled(ctx, db); e != nil || enabled {
		t.Fatalf("after disable=%t err=%v", enabled, e)
	}
	// 新控制器实例从库读回同一状态（跨进程持久语义）。
	fresh := New("module.test.enabled")
	if enabled, e = fresh.Enabled(ctx, db); e != nil || enabled {
		t.Fatalf("fresh read=%t err=%v", enabled, e)
	}
	if e = fresh.SetEnabled(ctx, db, gate, true); e != nil {
		t.Fatal(e)
	}
	if enabled, e = ctl.Enabled(ctx, db); e != nil || !enabled {
		t.Fatalf("ctl read after fresh write=%t err=%v", enabled, e)
	}
}
