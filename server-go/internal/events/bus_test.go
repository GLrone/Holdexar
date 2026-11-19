package events

import (
	"context"
	"testing"
	"time"
)

func ctxFor(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	t.Cleanup(cancel)
	return ctx
}

func TestPublishNormalDelivery(t *testing.T) {
	bus := NewBus()
	sub := bus.Subscribe()
	if bus.Len() != 1 {
		t.Fatalf("订阅后总数 = %d, want 1", bus.Len())
	}
	bus.Publish("job.started", KV{"job_id", 5}, KV{"scope", "appids"})
	bus.Publish("crawl.progress", KV{"done", 1})
	bus.Publish("job.status", KV{"status", "stopped"})

	got := make([]string, 0, 3)
	for range 3 {
		ev, err := sub.Get(ctxFor(t))
		if err != nil {
			t.Fatalf("Get: %v", err)
		}
		got = append(got, ev.Type)
	}
	want := []string{"job.started", "job.status", "crawl.progress"}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("事件序 = %v, want %v", got, want)
		}
	}
	if ev := got; len(ev) != 3 {
		t.Fatalf("收到 %d 条, want 3", len(ev))
	}
}

func TestBackpressureProgressDropsNewest(t *testing.T) {
	bus := NewBus()
	sub := bus.Subscribe()
	for i := 0; i < ProgressMax+10; i++ {
		bus.Publish("crawl.progress", KV{"done", i})
	}
	first, err := sub.Get(ctxFor(t))
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if first.Type != "crawl.progress" || first.Data[0].Val != 0 {
		t.Fatalf("首条进度 = %+v, want done=0", first)
	}
	last := first
	for i := 1; i < ProgressMax; i++ {
		last, err = sub.Get(ctxFor(t))
		if err != nil {
			t.Fatalf("Get #%d: %v", i, err)
		}
	}
	if last.Data[0].Val != ProgressMax-1 {
		t.Fatalf("progress 末条 = %v, want done=%d", last.Data[0].Val, ProgressMax-1)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if _, err := sub.Get(ctx); err == nil {
		t.Fatal("progress 通道读出超过容量（新事件未被丢弃）")
	}
}

func TestBackpressureCriticalEvictsOldest(t *testing.T) {
	bus := NewBus()
	sub := bus.Subscribe()
	for i := 0; i < CriticalMax+10; i++ {
		bus.Publish("job.status", KV{"seq", i})
	}
	ev, err := sub.Get(ctxFor(t))
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if ev.Data[0].Val != 10 {
		t.Fatalf("critical 首条 = %v, want seq=10（最旧被挤掉）", ev.Data[0].Val)
	}
	last := ev
	for i := 1; i < CriticalMax; i++ {
		last, err = sub.Get(ctxFor(t))
		if err != nil {
			t.Fatalf("Get #%d: %v", i, err)
		}
	}
	if last.Data[0].Val != CriticalMax+9 {
		t.Fatalf("critical 末条 = %v, want seq=%d（最新保留）", last.Data[0].Val, CriticalMax+9)
	}
}

func TestGetBlocksUntilEvent(t *testing.T) {
	bus := NewBus()
	sub := bus.Subscribe()
	done := make(chan Event, 1)
	go func() {
		ev, err := sub.Get(ctxFor(t))
		if err == nil {
			done <- ev
		}
	}()
	time.Sleep(30 * time.Millisecond)
	bus.Publish("job.started", KV{"job_id", 1})
	select {
	case ev := <-done:
		if ev.Type != "job.started" {
			t.Fatalf("收到 %s, want job.started", ev.Type)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("阻塞的 Get 未被发布唤醒")
	}
}

func TestGetRacingCriticalNeverLost(t *testing.T) {
	bus := NewBus()
	sub := bus.Subscribe()
	res := make(chan Event, 2)
	go func() {
		ev, err := sub.Get(ctxFor(t))
		if err == nil {
			res <- ev
		}
	}()
	time.Sleep(20 * time.Millisecond)
	bus.Publish("crawl.progress", KV{"done", 1})
	time.Sleep(5 * time.Millisecond)
	bus.Publish("job.status", KV{"status", "ok"})

	first := <-res
	second, err := sub.Get(ctxFor(t))
	if err != nil {
		t.Fatalf("第二次 Get: %v", err)
	}
	types := map[string]bool{first.Type: true, second.Type: true}
	if !types["job.status"] || !types["crawl.progress"] {
		t.Fatalf("竞速读取丢事件：got %s + %s", first.Type, second.Type)
	}
}

func TestUnsubscribeNoLeak(t *testing.T) {
	bus := NewBus()
	subs := make([]*Subscriber, 0, 8)
	for range 8 {
		subs = append(subs, bus.Subscribe())
	}
	if bus.Len() != 8 {
		t.Fatalf("订阅数 = %d, want 8", bus.Len())
	}
	for _, sub := range subs {
		bus.Unsubscribe(sub)
	}
	if bus.Len() != 0 {
		t.Fatalf("注销后订阅数 = %d, want 0", bus.Len())
	}
	bus.Unsubscribe(subs[0])
	bus.Publish("job.status", KV{"status", "ok"})
	if bus.Len() != 0 {
		t.Fatalf("发布后订阅数 = %d, want 0", bus.Len())
	}
}

func TestUnsubscribeStopsDelivery(t *testing.T) {
	bus := NewBus()
	sub := bus.Subscribe()
	bus.Unsubscribe(sub)
	bus.Publish("job.status", KV{"status", "ok"})
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if _, err := sub.Get(ctx); err == nil {
		t.Fatal("注销后仍收到事件")
	}
}
