package occupancy

import (
	"errors"
	"fmt"
	"runtime"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// resetState 全局状态清场：包内测试直接复位，测试间互不残留。
func resetState() {
	mu.Lock()
	holder = ""
	maintenanceKind = ""
	mu.Unlock()
}

// expectBusy 断言被拒、可哨兵判别且拒因一致。
func expectBusy(t *testing.T, err error, want string) {
	t.Helper()
	var be *BusyError
	if !errors.As(err, &be) {
		t.Fatalf("应为占用冲突错误，实际 %v", err)
	}
	if !errors.Is(err, ErrCrawlerBusy) {
		t.Fatalf("占用冲突错误应可 errors.Is 判别，实际 %v", err)
	}
	if be.Reason != want {
		t.Fatalf("拒因应为 %q，实际 %q", want, be.Reason)
	}
}

// ── 互斥矩阵：爬取 ↔ 维护互拒，正常释放后可再入 ──────────────────

func TestCrawlMaintenanceMutex(t *testing.T) {
	resetState()
	defer resetState()

	if err := BeginCrawl("job-1"); err != nil {
		t.Fatalf("空闲态取得爬取占用失败: %v", err)
	}
	if !CrawlerBusy() || CurrentHolder() != "job-1" {
		t.Fatalf("爬取占用应在位，holder=%q", CurrentHolder())
	}
	// 爬取在位：维护拒绝、异 tag 拒绝、同 tag 幂等
	expectBusy(t, BeginMaintenance(MaintenanceHealth), "已有爬取任务在执行（job-1）")
	expectBusy(t, BeginCrawl("job-2"), "已有爬取任务在执行（job-1）")
	if err := BeginCrawl("job-1"); err != nil {
		t.Fatalf("同 tag 重入应幂等: %v", err)
	}

	// 释放后维护可入；维护在位：双向拒绝
	EndCrawl()
	if CrawlerBusy() {
		t.Fatalf("释放后不应为忙")
	}
	if err := BeginMaintenance(MaintenanceHealth); err != nil {
		t.Fatalf("释放后取得维护预约失败: %v", err)
	}
	if !MaintenanceActive() || MaintenanceKind() != MaintenanceHealth ||
		MaintenanceReason() != "代理体检进行中" {
		t.Fatalf("体检预约应在位，kind=%q reason=%q", MaintenanceKind(), MaintenanceReason())
	}
	// 预约不幂等：同种类重入同样拒
	expectBusy(t, BeginMaintenance(MaintenanceHealth), "代理体检进行中")
	expectBusy(t, BeginCrawl("job-3"), "代理体检进行中")
	expectBusy(t, BeginMaintenance(MaintenanceRebuild), "代理体检进行中")

	// 清预约后换种类，拒因随之切换
	EndMaintenance()
	if err := BeginMaintenance(MaintenanceRebuild); err != nil {
		t.Fatalf("清理后取得重建预约失败: %v", err)
	}
	expectBusy(t, BeginCrawl("job-4"), "代理通道正在重建")

	// 全部释放后爬取可再入，拒因读点归零
	EndMaintenance()
	if err := BeginCrawl("job-5"); err != nil {
		t.Fatalf("维护清理后取得爬取占用失败: %v", err)
	}
	if MaintenanceReason() != "" {
		t.Fatalf("无预约时拒因应为空，实际 %q", MaintenanceReason())
	}
}

func TestEndWithoutBeginIdempotent(t *testing.T) {
	resetState()
	defer resetState()

	EndCrawl()
	EndMaintenance()
	if CrawlerBusy() || MaintenanceActive() {
		t.Fatalf("未占位时的释放应为空操作")
	}
	if err := BeginCrawl("job-1"); err != nil {
		t.Fatalf("空操作后应可取得占用: %v", err)
	}
}

// ── 异常路径不泄漏：调用方 defer 配对释放 ────────────────────────

// holdThenError 占用后以业务错误返回，释放靠调用方 defer。
func holdThenError() error {
	if err := BeginCrawl("leaky"); err != nil {
		return err
	}
	defer EndCrawl()
	return errors.New("boom")
}

func TestDeferReleaseOnErrorPath(t *testing.T) {
	resetState()
	defer resetState()

	if err := holdThenError(); err == nil {
		t.Fatalf("应透传业务错误")
	}
	if CrawlerBusy() || CurrentHolder() != "" {
		t.Fatalf("异常返回不得泄漏占用，holder=%q", CurrentHolder())
	}
	if err := BeginCrawl("next"); err != nil {
		t.Fatalf("清理后应可再入: %v", err)
	}
}

func TestDeferReleaseOnPanicPath(t *testing.T) {
	resetState()
	defer resetState()

	func() {
		defer func() { _ = recover() }()
		if err := BeginMaintenance(MaintenanceHealth); err != nil {
			t.Errorf("取得维护预约失败: %v", err)
			return
		}
		defer EndMaintenance()
		panic("boom")
	}()
	if MaintenanceActive() || CrawlerBusy() {
		t.Fatalf("panic 不得泄漏维护预约，kind=%q", MaintenanceKind())
	}
	if err := BeginCrawl("after-panic"); err != nil {
		t.Fatalf("panic 清理后应可再入: %v", err)
	}
}

func TestBusyErrorSentinelOnly(t *testing.T) {
	resetState()
	defer resetState()

	if errors.Is(errors.New("无关错误"), ErrCrawlerBusy) {
		t.Fatalf("普通错误不应判别为占用冲突")
	}
}

// ── 并发压测：多 goroutine 竞争，无死锁无双持 ────────────────────

func TestConcurrentContention(t *testing.T) {
	resetState()
	defer resetState()

	const (
		goroutines = 8
		iterations = 200
	)
	var (
		holdsCrawl int32
		holdsMaint int32
		wg         sync.WaitGroup
	)

	done := make(chan struct{})
	go func() {
		defer close(done)
		for g := 0; g < goroutines; g++ {
			wg.Add(1)
			go func(id int) {
				defer wg.Done()
				tag := fmt.Sprintf("crawl-%d", id)
				// 计数窗口须嵌在持位区间内：减计先于释放，释放间隙不算在位
				for i := 0; i < iterations; i++ {
					if i%2 == 0 {
						if err := BeginCrawl(tag); err == nil {
							if n := atomic.AddInt32(&holdsCrawl, 1); n != 1 {
								t.Errorf("并发双持爬取占用：在位 %d", n)
							}
							if CurrentHolder() != tag {
								t.Errorf("持位读点应为本 goroutine，实际 %q", CurrentHolder())
							}
							runtime.Gosched()
							atomic.AddInt32(&holdsCrawl, -1)
							EndCrawl()
						} else if !errors.Is(err, ErrCrawlerBusy) {
							t.Errorf("并发拒绝应只是占用冲突: %v", err)
						}
					} else {
						kind := MaintenanceHealth
						if i%4 == 3 {
							kind = MaintenanceRebuild
						}
						if err := BeginMaintenance(kind); err == nil {
							if n := atomic.AddInt32(&holdsMaint, 1); n != 1 {
								t.Errorf("并发双持维护预约：在位 %d", n)
							}
							runtime.Gosched()
							atomic.AddInt32(&holdsMaint, -1)
							EndMaintenance()
						} else if !errors.Is(err, ErrCrawlerBusy) {
							t.Errorf("并发拒绝应只是占用冲突: %v", err)
						}
					}
				}
			}(g)
		}
		wg.Wait()
	}()

	select {
	case <-done:
	case <-time.After(30 * time.Second):
		t.Fatal("并发互斥疑似死锁，30s 未完成")
	}

	if n := atomic.LoadInt32(&holdsCrawl) + atomic.LoadInt32(&holdsMaint); n != 0 {
		t.Fatalf("并发结束后在位计数应归零，实际 %d", n)
	}
	if CrawlerBusy() || CurrentHolder() != "" || MaintenanceActive() {
		t.Fatalf("并发结束后应全部清场，holder=%q kind=%q", CurrentHolder(), MaintenanceKind())
	}
}
