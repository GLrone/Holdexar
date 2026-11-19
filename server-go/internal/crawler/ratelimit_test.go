package crawler

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

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

type manualClock struct {
	mu     sync.Mutex
	now    time.Time
	wake   chan struct{}
	pend   time.Time
	sleeps atomic.Int64
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

func (c *manualClock) SleepCount() int64 { return c.sleeps.Load() }

func (c *manualClock) PendingDeadline() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.pend
}

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

type errSleepClock struct {
	advancingClock
	err error
}

func (c *errSleepClock) Sleep(_ context.Context, _ time.Duration) error { return c.err }

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
	_ = lim.Acquire(ctx)

	var wg sync.WaitGroup
	errs := make([]error, 2)
	for i := 0; i < 2; i++ {
		wg.Go(func() {
			errs[i] = lim.Acquire(ctx)
		})
	}
	waitFor(t, "两个等待者进入排队", func() bool {
		return clk.SleepCount() == 2 && lim.Waiting() == 2
	})
	clk.AdvanceTo(clk.PendingDeadline())
	waitFor(t, "第二轮等待只计一人", func() bool {
		return clk.SleepCount() == 3 && lim.Waiting() == 1
	})
	clk.AdvanceTo(clk.PendingDeadline())
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
	_ = lim.Acquire(context.Background())
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
	exits2 := NewExitRateLimits([]string{"", ""}, 5, time.Second, fallback)
	if exits2.ExitCount() != 0 {
		t.Fatalf("空串出口键不应建账，实际 %d", exits2.ExitCount())
	}
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
