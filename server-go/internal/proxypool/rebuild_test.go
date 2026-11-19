package proxypool

import (
	"context"
	"errors"
	"testing"
	"time"

	"holdexar/server-go/internal/occupancy"
)

func TestFailedPendingRebuildRetriesOnNextCycle(t *testing.T) {
	defer TakeRebuildPending()
	RequestRebuild()
	failure := errors.New("controlled kernel failure")
	if ran, err := RunPendingRebuild(context.Background(), func(context.Context) error { return failure }); !ran || !errors.Is(err, failure) {
		t.Fatalf("ran=%v err=%v", ran, err)
	}
	if ran, err := RunPendingRebuild(context.Background(), func(context.Context) error { return nil }); !ran || err != nil {
		t.Fatalf("rebuild was not retried: ran=%v err=%v", ran, err)
	}
	if RebuildPending() {
		t.Fatal("successful rebuild left a pending request")
	}
}

func TestRebuildPendingStateMachine(t *testing.T) {
	defer rebuildPending.Store(false)
	rebuildPending.Store(false)

	if RebuildPending() {
		t.Fatal("初始不应待重建")
	}
	RequestRebuild()
	RequestRebuild()
	if !RebuildPending() {
		t.Fatal("置位后应待重建")
	}
	if !TakeRebuildPending() {
		t.Fatal("首次消费应返回待重建")
	}
	if RebuildPending() || TakeRebuildPending() {
		t.Fatal("消费后应清空")
	}
}

func TestRunPendingRebuildGivesWayWhenBusy(t *testing.T) {
	defer rebuildPending.Store(false)
	rebuildPending.Store(false)
	defer occupancy.EndCrawl()
	defer occupancy.EndMaintenance()

	RequestRebuild()
	if err := occupancy.BeginCrawl("test-crawl"); err != nil {
		t.Fatal(err)
	}
	ran, err := RunPendingRebuild(context.Background(), func(ctx context.Context) error {
		t.Fatal("爬取在位不得动手")
		return nil
	})
	if err != nil || ran {
		t.Fatalf("爬取在位应让路：ran=%v err=%v", ran, err)
	}
	if !RebuildPending() {
		t.Fatal("让路时待办位应保留")
	}
	occupancy.EndCrawl()
	if err := occupancy.BeginMaintenance(occupancy.MaintenanceHealth); err != nil {
		t.Fatal(err)
	}
	ran, err = RunPendingRebuild(context.Background(), func(ctx context.Context) error {
		t.Fatal("体检预约在位不得动手")
		return nil
	})
	if err != nil || ran {
		t.Fatalf("体检预约在位应让路：ran=%v err=%v", ran, err)
	}
	if !RebuildPending() {
		t.Fatal("让路时待办位应保留")
	}
	occupancy.EndMaintenance()
	ran, err = RunPendingRebuild(context.Background(), func(ctx context.Context) error { return nil })
	if err != nil || !ran {
		t.Fatalf("空闲应消费：ran=%v err=%v", ran, err)
	}
	if RebuildPending() {
		t.Fatal("消费后应清空")
	}
}

func TestRunPendingRebuildNoopCases(t *testing.T) {
	defer rebuildPending.Store(false)
	rebuildPending.Store(false)

	calls := 0
	ran, err := RunPendingRebuild(context.Background(), func(ctx context.Context) error {
		calls++
		return nil
	})
	if err != nil || ran || calls != 0 {
		t.Fatalf("无待办应空手：ran=%v err=%v calls=%d", ran, err, calls)
	}
	RequestRebuild()
	ran, err = RunPendingRebuild(context.Background(), nil)
	if err != nil || ran {
		t.Fatalf("nil 回调应返回未执行：ran=%v err=%v", ran, err)
	}
	if RebuildPending() {
		t.Fatal("nil 回调也应消费待办位")
	}
}

func TestRunPendingRebuildConsumesBeforeAction(t *testing.T) {
	defer rebuildPending.Store(false)
	rebuildPending.Store(false)

	RequestRebuild()
	ran, err := RunPendingRebuild(context.Background(), func(ctx context.Context) error {
		RequestRebuild()
		return nil
	})
	if err != nil || !ran {
		t.Fatalf("应执行重建：ran=%v err=%v", ran, err)
	}
	if !RebuildPending() {
		t.Fatal("重建中新产生的 pending 应保留给下一拍")
	}
	TakeRebuildPending()
}

func TestEndCrawlKicksOnCrawlEnd(t *testing.T) {
	prev := occupancy.OnCrawlEnd
	defer func() { occupancy.OnCrawlEnd = prev }()

	fired := make(chan string, 1)
	occupancy.OnCrawlEnd = func() {
		fired <- occupancy.CurrentHolder()
	}
	if err := occupancy.BeginCrawl("kick-test"); err != nil {
		t.Fatal(err)
	}
	occupancy.EndCrawl()
	select {
	case holder := <-fired:
		if holder != "" {
			t.Fatalf("钩子执行时占用应已释放，实际持有者 %q", holder)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("释放后钩子未触发")
	}
}
