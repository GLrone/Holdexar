package crawler

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// ── 测试时钟 ─────────────────────────────────────────────────────

// advancingClock：Sleep 直接推进时钟，用于单 goroutine 的窗口/冷却序列测试。
type advancingClock struct {
	mu     sync.Mutex
	now    time.Time
	sleeps []time.Duration
}

func newAdvancingClock() *advancingClock { return &advancingClock{now: time.Unix(0, 0)} }

func (c *advancingClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *advancingClock) Sleep(_ context.Context, d time.Duration) error {
	c.mu.Lock()
	c.sleeps = append(c.sleeps, d)
	c.now = c.now.Add(d)
	c.mu.Unlock()
	return nil
}

func (c *advancingClock) sleepCount() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.sleeps)
}

// manualClock：Sleep 阻塞到时钟到达等待截止时刻，用于并发等待观测。
// 每次进入 Sleep 登记进入计数与截止时刻，推进方用 AdvanceTo 精确推到
// 截止点，避免「推进早于等待者读起点」的竞态。
type manualClock struct {
	mu     sync.Mutex
	now    time.Time
	wake   chan struct{}
	pend   time.Time    // 最近一次 Sleep 的截止时刻（观测口）
	sleeps atomic.Int64 // Sleep 进入次数（观测口）
}

func newManualClock() *manualClock {
	return &manualClock{now: time.Unix(0, 0), wake: make(chan struct{})}
}

func (c *manualClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *manualClock) Sleep(ctx context.Context, d time.Duration) error {
	c.mu.Lock()
	deadline := c.now.Add(d)
	c.pend = deadline
	ch := c.wake
	c.mu.Unlock()
	c.sleeps.Add(1)
	for {
		c.mu.Lock()
		now := c.now
		c.mu.Unlock()
		if !now.Before(deadline) {
			return nil
		}
		select {
		case <-ch:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

// SleepCount：Sleep 累计进入次数。
func (c *manualClock) SleepCount() int64 { return c.sleeps.Load() }

// PendingDeadline：最近一次进入 Sleep 的截止时刻。
func (c *manualClock) PendingDeadline() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.pend
}

// AdvanceTo：时钟前进到 t（只进不退），唤醒全部等待者重新判定。
func (c *manualClock) AdvanceTo(t time.Time) {
	c.mu.Lock()
	if t.After(c.now) {
		c.now = t
	}
	ch := c.wake
	c.wake = make(chan struct{})
	c.mu.Unlock()
	close(ch)
}

// errSleepClock：Now 正常，Sleep 返回注入错误（fail-open 观测）。
type errSleepClock struct {
	advancingClock
	err error
}

func (c *errSleepClock) Sleep(_ context.Context, _ time.Duration) error { return c.err }

// waitFor：轮询条件成立，超时判失败（避免真实长等待）。
func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("等待超时：%s", what)
}

// ── 滑窗限流 ─────────────────────────────────────────────────────

func TestRateLimiterUnderWindow(t *testing.T) {
	clk := newAdvancingClock()
	lim := NewRateLimiter(2, time.Second)
	lim.clock = clk
	for i := 0; i < 2; i++ {
		if err := lim.Acquire(context.Background()); err != nil {
			t.Fatalf("窗口内取号失败: %v", err)
		}
	}
	if clk.sleepCount() != 0 {
		t.Fatalf("窗口内取号不应等待，实际等待 %d 次", clk.sleepCount())
	}
}

func TestRateLimiterWindowBoundary(t *testing.T) {
	clk := newAdvancingClock()
	lim := NewRateLimiter(2, time.Second)
	lim.clock = clk
	_ = lim.Acquire(context.Background())
	_ = lim.Acquire(context.Background())
	// 时刻恰好到窗：最旧时间戳等于出窗界，应被清理并直接放行
	clk.mu.Lock()
	clk.now = clk.now.Add(time.Second)
	clk.mu.Unlock()
	if err := lim.Acquire(context.Background()); err != nil {
		t.Fatalf("窗口边界取号失败: %v", err)
	}
	if clk.sleepCount() != 0 {
		t.Fatalf("窗口边界不应等待，实际等待 %d 次", clk.sleepCount())
	}
}

func TestRateLimiterWaitUntilOldestExits(t *testing.T) {
	clk := newAdvancingClock()
	lim := NewRateLimiter(1, time.Second)
	lim.clock = clk
	_ = lim.Acquire(context.Background())
	clk.mu.Lock()
	clk.now = clk.now.Add(200 * time.Millisecond)
	clk.mu.Unlock()
	if err := lim.Acquire(context.Background()); err != nil {
		t.Fatalf("窗口满取号失败: %v", err)
	}
	if clk.sleepCount() != 1 || clk.sleeps[0] != 800*time.Millisecond {
		t.Fatalf("应恰好等待最旧出窗的 800ms，实际 %v", clk.sleeps)
	}
	if got := lim.Waiting(); got != 0 {
		t.Fatalf("等待结束后排队计数应归零，实际 %d", got)
	}
}

func TestRateLimiterWaitingCountedOnce(t *testing.T) {
	clk := newManualClock()
	lim := NewRateLimiter(1, time.Second)
	lim.clock = clk
	ctx := context.Background()
	_ = lim.Acquire(ctx) // 占满窗口

	var wg sync.WaitGroup
	errs := make([]error, 2)
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			errs[i] = lim.Acquire(ctx)
		}(i)
	}
	waitFor(t, "两个等待者进入排队", func() bool {
		return clk.SleepCount() == 2 && lim.Waiting() == 2
	})
	clk.AdvanceTo(clk.PendingDeadline()) // 推到 1s：一人得名额，另一人进入第二轮等待
	// 第二轮等待不重复计数：仍在排队的只有一人
	waitFor(t, "第二轮等待只计一人", func() bool {
		return clk.SleepCount() == 3 && lim.Waiting() == 1
	})
	clk.AdvanceTo(clk.PendingDeadline()) // 推到 2s：第二轮等待者得名额
	wg.Wait()
	for i, err := range errs {
		if err != nil {
			t.Fatalf("等待者 %d 取号失败: %v", i, err)
		}
	}
	if got := lim.Waiting(); got != 0 {
		t.Fatalf("全部取号后排队计数应归零，实际 %d", got)
	}
}

func TestRateLimiterFailOpen(t *testing.T) {
	clk := &errSleepClock{err: errors.New("时钟故障")}
	lim := NewRateLimiter(1, time.Second)
	lim.clock = clk
	_ = lim.Acquire(context.Background()) // 占满窗口
	// 内部故障：放行且不占名额、不排队
	if err := lim.Acquire(context.Background()); err != nil {
		t.Fatalf("内部故障应放行而非报错: %v", err)
	}
	if got := lim.Waiting(); got != 0 {
		t.Fatalf("放行路径不应计入排队，实际 %d", got)
	}
	lim.mu.Lock()
	n := len(lim.stamps)
	lim.mu.Unlock()
	if n != 1 {
		t.Fatalf("放行路径不应记录名额，实际 %d 条", n)
	}

	// 调用方取消：原样上抛
	clk2 := newManualClock()
	lim2 := NewRateLimiter(1, time.Second)
	lim2.clock = clk2
	_ = lim2.Acquire(context.Background())
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := lim2.Acquire(ctx); !errors.Is(err, context.Canceled) {
		t.Fatalf("取消应上抛，实际 %v", err)
	}
}

// ── 按出口分账 ───────────────────────────────────────────────────

func TestExitRateLimitsIsolation(t *testing.T) {
	fallback := NewRateLimiter(1, time.Second)
	exits := NewExitRateLimits([]string{"a", "a", "b"}, 2, time.Second, fallback)
	if exits.ExitCount() != 2 {
		t.Fatalf("重复出口键应只建一份预算，实际 %d", exits.ExitCount())
	}
	if exits.PerExit() != 2 {
		t.Fatalf("单出口预算应为 2，实际 %d", exits.PerExit())
	}
	ctx := context.Background()
	a := exits.ForExit("a")
	_ = a.Acquire(ctx)
	_ = a.Acquire(ctx)
	// 出口 a 预算占满：同键继续等，出口 b 不受影响
	aCtx, cancel := context.WithTimeout(ctx, 50*time.Millisecond)
	defer cancel()
	if err := a.Acquire(aCtx); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("出口 a 满窗应阻塞，实际 %v", err)
	}
	if err := exits.ForExit("b").Acquire(ctx); err != nil {
		t.Fatalf("出口 b 不应受 a 占满影响: %v", err)
	}
}

func TestExitRateLimitsFallback(t *testing.T) {
	fallback := NewRateLimiter(1, time.Second)
	exits := NewExitRateLimits(nil, 5, time.Second, fallback)
	if exits.ExitCount() != 0 {
		t.Fatalf("空键清单应无分账，实际 %d", exits.ExitCount())
	}
	if exits.ForExit("") != fallback || exits.ForExit("x") != fallback {
		t.Fatalf("空账本应全部回落共享实例")
	}
	// 空键不建账：显式传入的空串被跳过
	exits2 := NewExitRateLimits([]string{"", ""}, 5, time.Second, fallback)
	if exits2.ExitCount() != 0 {
		t.Fatalf("空串出口键不应建账，实际 %d", exits2.ExitCount())
	}
	// 未知键回落
	exits3 := NewExitRateLimits([]string{"a"}, 5, time.Second, fallback)
	if exits3.ForExit("unknown") != fallback {
		t.Fatalf("未知出口键应回落共享实例")
	}
}

func TestDefaultRateLimiterSingleton(t *testing.T) {
	if DefaultRateLimiter.maxRequests != DefaultRateMaxRequests {
		t.Fatalf("进程级单例预算应为 %d，实际 %d", DefaultRateMaxRequests, DefaultRateLimiter.maxRequests)
	}
}
