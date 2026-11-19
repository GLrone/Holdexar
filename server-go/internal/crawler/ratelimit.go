// 按出口 IP 分账限流（未知或空键回落进程级单例），同出口多 worker 共享预算。
// 非 ctx 取消的内部故障直接放行，交由重试与熔断兜底。
package crawler

import (
	"context"
	"errors"
	"strings"
	"sync"
	"time"
)

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

const (
	DefaultRateMaxRequests = 200
	DefaultRateWindow      = 300 * time.Second
)

var DefaultRateLimiter = NewRateLimiter(DefaultRateMaxRequests, DefaultRateWindow)

type RateLimiter struct {
	maxRequests int
	window      time.Duration
	clock       SleepClock

	mu      sync.Mutex
	stamps  []time.Time
	waiting int
}

func NewRateLimiter(maxRequests int, window time.Duration) *RateLimiter {
	return &RateLimiter{
		maxRequests: maxRequests,
		window:      window,
		clock:       systemClock{},
	}
}

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
			return nil
		}
	}
}

func (r *RateLimiter) Waiting() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.waiting
}

func (r *RateLimiter) Reset() {
	r.mu.Lock()
	r.stamps = nil
	r.mu.Unlock()
}

// ExitRateLimits 按出口 IP 维护独立限流器，同出口多 worker 共享预算。
type ExitRateLimits struct {
	per      map[string]*RateLimiter
	fallback *RateLimiter
}

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

func (e *ExitRateLimits) ForExit(key string) *RateLimiter {
	if len(e.per) == 0 {
		return e.fallback
	}
	if l, ok := e.per[key]; ok {
		return l
	}
	return e.fallback
}

func (e *ExitRateLimits) ExitCount() int { return len(e.per) }

func (e *ExitRateLimits) PerExit() int {
	for _, l := range e.per {
		return l.maxRequests
	}
	return DefaultRateMaxRequests
}
