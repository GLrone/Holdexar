package store

import (
	"context"
	"testing"
	"time"
)

// TestWriteGateDiagnosticsStates 三态钉住：空闲 / 持闸 / 双优先级排队。
// 键名对齐 Python write_scheduler_diagnostics（busy/depth/owner_priority/
// owner_label/waiting_interactive/waiting_background + pool）。
func TestWriteGateDiagnosticsStates(t *testing.T) {
	db, path := openTestDB(t)
	if _, err := db.Exec("CREATE TABLE counter(n INTEGER)"); err != nil {
		t.Fatal(err)
	}
	g := NewWriteGate(db, path)
	ctx := context.Background()

	// 空闲：无持闸者、无排队、pool 段在场
	snap := g.Diagnostics(ctx)
	if snap["busy"] != false || snap["owner_priority"] != nil || snap["owner_label"] != nil ||
		snap["waiting_interactive"] != 0 || snap["waiting_background"] != 0 {
		t.Fatalf("空闲快照=%v", snap)
	}
	pool, ok := snap["pool"].(map[string]any)
	if !ok || pool["type"] != "*sql.DB" {
		t.Fatalf("pool 段缺失或形状不对：%v", snap["pool"])
	}

	// 持闸：交互写者在位
	lease, err := g.Acquire(ctx, PriorityInteractive)
	if err != nil {
		t.Fatal(err)
	}
	snap = waitDiag(t, g, ctx, func(s map[string]any) bool { return s["busy"] == true })
	if snap["owner_priority"] != "INTERACTIVE" || snap["owner_label"] != nil || snap["depth"] != 1 {
		t.Fatalf("持闸快照=%v", snap)
	}
	if snap["waiting_interactive"] != 0 || snap["waiting_background"] != 0 {
		t.Fatalf("持闸但不应有排队：%v", snap)
	}

	// 排队：1 交互 + 2 后台（持闸者未释放，后来者入队）
	queued := make(chan error, 3)
	for _, prio := range []WritePriority{PriorityBackground, PriorityInteractive, PriorityBackground} {
		go func(p WritePriority) {
			l, aerr := g.Acquire(ctx, p)
			if aerr == nil {
				l.Done()
			}
			queued <- aerr
		}(prio)
	}
	snap = waitDiag(t, g, ctx, func(s map[string]any) bool {
		return s["waiting_interactive"] == 1 && s["waiting_background"] == 2
	})
	if snap["busy"] != true || snap["owner_priority"] != "INTERACTIVE" {
		t.Fatalf("排队期快照=%v", snap)
	}

	lease.Done()
	for i := 0; i < 3; i++ {
		if err := <-queued; err != nil {
			t.Fatalf("排队写者失败：%v", err)
		}
	}
	snap = waitDiag(t, g, ctx, func(s map[string]any) bool {
		return s["busy"] == false && s["waiting_interactive"] == 0 && s["waiting_background"] == 0
	})
	if snap["owner_priority"] != nil || snap["depth"] != 0 {
		t.Fatalf("收尾快照=%v", snap)
	}
}

func waitDiag(t *testing.T, g *WriteGate, ctx context.Context, cond func(map[string]any) bool) map[string]any {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for {
		snap := g.Diagnostics(ctx)
		if snap != nil && cond(snap) {
			return snap
		}
		if time.Now().After(deadline) {
			t.Fatalf("诊断快照未达预期：%v", snap)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func TestWriteGateDiagnosticsContextCancel(t *testing.T) {
	db, path := openTestDB(t)
	g := NewWriteGate(db, path)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if snap := g.Diagnostics(ctx); snap != nil {
		t.Fatalf("已取消 ctx 应返回 nil：%v", snap)
	}
}
