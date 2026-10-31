// 出网滑窗限流：窗口内最多 max 次请求，超出即等到最旧请求出窗。
//
// 限流按出口键分账：同出口上的多个 lane/worker 共享同一份预算，总额度随
// 出口数增长；未知或空出口键回落进程级单例（单入口形态共用一份预算）。
//
// 故障口径：限流器内部出错（等待被打断以外的时钟/休眠故障）直接放行本次
// 请求——它只负责匀速，故障时宁可让请求发出去，交给既有重试与熔断兜底；
// 调用方取消（ctx 结束）则原样返回错误。
package crawler

import (
	"context"
	"errors"
	"strings"
	"sync"
	"time"
)

// Clock：单调时钟抽象，测试注入模拟时钟。
type Clock interface {
	Now() time.Time
}

// SleepClock：Clock 加可打断等待。等待实现必须尊重 ctx：ctx 结束即返回其错误。
type SleepClock interface {
	Clock
	Sleep(ctx context.Context, d time.Duration) error
}

type systemClock struct{}

func (systemClock) Now() time.Time { return time.Now() }

func (systemClock) Sleep(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
			return nil
		}
	}
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

// 窗口参数：当前生产预算口径（200 发 / 300 秒，按出口 IP 一份）。
const (
	DefaultRateMaxRequests = 200
	DefaultRateWindow      = 300 * time.Second
)

// DefaultRateLimiter：进程级单例。无出口键的调用方（非 lane 通道）回落到它，
// 与 lane 爬取共享同一份窗口预算。
var DefaultRateLimiter = NewRateLimiter(DefaultRateMaxRequests, DefaultRateWindow)

// RateLimiter：滑动窗口限流器。
type RateLimiter struct {
	maxRequests int
	window      time.Duration
	clock       SleepClock

	mu      sync.Mutex
	stamps  []time.Time
	waiting int
}

// NewRateLimiter：maxRequests 为窗口内上限，window 为窗口时长。
func NewRateLimiter(maxRequests int, window time.Duration) *RateLimiter {
	return &RateLimiter{
		maxRequests: maxRequests,
		window:      window,
		clock:       systemClock{},
	}
}

// Acquire：取一个请求名额；窗口满时阻塞到最旧请求出窗。
//
// 等待用循环重取：睡醒后名额可能再被抢走，重取静默进行；排队计数只在
// 首次进入等待时加一，退出时减一。ctx 取消原样上抛，其余内部故障放行。
func (r *RateLimiter) Acquire(ctx context.Context) error {
	if ctx == nil {
		ctx = context.Background()
	}
	waited := false
	defer func() {
		if waited {
			r.mu.Lock()
			r.waiting--
			r.mu.Unlock()
		}
	}()
	for {
		now := r.clock.Now()
		r.mu.Lock()
		cutoff := now.Add(-r.window)
		i := 0
		for i < len(r.stamps) && !r.stamps[i].After(cutoff) {
			i++
		}
		r.stamps = r.stamps[i:]
		if len(r.stamps) < r.maxRequests {
			r.stamps = append(r.stamps, now)
			r.mu.Unlock()
			return nil
		}
		// 窗口满：最旧一条出窗的时刻即最早可发时刻
		wait := r.stamps[0].Add(r.window).Sub(now)
		if !waited {
			r.waiting++
			waited = true
		}
		r.mu.Unlock()
		if err := r.clock.Sleep(ctx, wait); err != nil {
			if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
				return err
			}
			return nil // 限流器内部故障，本次放行
		}
	}
}

// Waiting：当前在窗口外排队的请求数（0 = 所有请求即取即发）。
func (r *RateLimiter) Waiting() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.waiting
}

// Reset：清空窗口（测试隔离用）。
func (r *RateLimiter) Reset() {
	r.mu.Lock()
	r.stamps = nil
	r.mu.Unlock()
}

// ExitRateLimits：按出口键分账的限流账本。
//
// 账本键是出口 IP（不是 lane 序号、也不是 worker 序号）：同一出口上放几个
// worker 都只吃这一份预算。总额度随出口数增长——每个出口各自面对上游的
// 那条频率线；全局异常由 429 熔断器兜底，不靠收紧限流。
type ExitRateLimits struct {
	per      map[string]*RateLimiter
	fallback *RateLimiter
}

// NewExitRateLimits：为每个非空出口键建一份预算；同键重复出现只建一份。
// fallback 为空/未知出口键的回落限流器，nil 时用进程级单例。
func NewExitRateLimits(exitKeys []string, maxRequests int, window time.Duration, fallback *RateLimiter) *ExitRateLimits {
	per := make(map[string]*RateLimiter, len(exitKeys))
	for _, k := range exitKeys {
		k = strings.TrimSpace(k)
		if k == "" {
			continue
		}
		if _, dup := per[k]; dup {
			continue
		}
		per[k] = NewRateLimiter(maxRequests, window)
	}
	if fallback == nil {
		fallback = DefaultRateLimiter
	}
	return &ExitRateLimits{per: per, fallback: fallback}
}

// ForExit：取出口键对应的限流器；无账本或未知键回落共享实例。
func (e *ExitRateLimits) ForExit(key string) *RateLimiter {
	if len(e.per) == 0 {
		return e.fallback
	}
	if l, ok := e.per[key]; ok {
		return l
	}
	return e.fallback
}

// ExitCount：分账的出口数。
func (e *ExitRateLimits) ExitCount() int { return len(e.per) }

// PerExit：单个出口的窗口上限（同键同值，供测试与日志断言）。
func (e *ExitRateLimits) PerExit() int {
	for _, l := range e.per {
		return l.maxRequests
	}
	return DefaultRateMaxRequests
}
