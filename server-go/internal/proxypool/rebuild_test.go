// 重建待办位与消费链单测：状态机语义、占用让路、消费在动手之前、
// 占用释放触发钩子。真内核热重建走 live 测试。
package proxypool

import (
	"context"
	"testing"
	"time"

	"holdexar/server-go/internal/occupancy"
)

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
	// 无待办：空手返回，回调不动。
	ran, err := RunPendingRebuild(context.Background(), func(ctx context.Context) error {
		calls++
		return nil
	})
	if err != nil || ran || calls != 0 {
		t.Fatalf("无待办应空手：ran=%v err=%v calls=%d", ran, err, calls)
	}
	// 无回调（未接线）：消费但不执行。
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

	// 重建中再次变脏：pending 属下一次重建，不被本次吞掉。
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

	// 钩子在占用已释放后异步执行（Python create_task 同款）：触发时持有者应已清空。
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
