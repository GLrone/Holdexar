package jobrun

import (
	"context"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
)

// RunManual 手动触发记账：trigger=manual + 入口 ref 进 meta（对齐
// scheduler_bridge.run_accounted trigger="manual" 的 run 行形状）。
func TestRunManualTriggerAndEntry(t *testing.T) {
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, e := store.Open(path)
	if e != nil {
		t.Fatal(e)
	}
	defer db.Close()
	if e = store.InitializeFreshDatabase(context.Background(), db); e != nil {
		t.Fatal(e)
	}
	r := New(db, store.NewWriteGate(db, path), time.Now, func(string, ...any) {})
	if _, e = r.RunManual(context.Background(), "bundle_refresh", "manual_refresh_button",
		func() (map[string]any, error) { return map[string]any{"ok": true}, nil }); e != nil {
		t.Fatal(e)
	}
	var trigger, meta string
	if e = db.QueryRow(`SELECT "trigger",meta FROM agent_runs WHERE runner='job' AND status='done' ORDER BY created_at DESC LIMIT 1`).
		Scan(&trigger, &meta); e != nil {
		t.Fatal(e)
	}
	if trigger != "manual" {
		t.Fatalf("trigger=%s，应为 manual", trigger)
	}
	for _, fragment := range []string{`"job":"bundle_refresh"`, `"entry":"manual_refresh_button"`} {
		if !strings.Contains(meta, fragment) {
			t.Fatalf("meta 缺 %s：%s", fragment, meta)
		}
	}
}
