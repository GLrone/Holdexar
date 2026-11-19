// 429 熔断器：按出口隔离，连续真 429 触发持锁冷却（5s 起指数翻倍，60s 封顶），
// 到期不自动复位，须放行试探成功一次才归零。
package crawler

import (
	"context"
	"encoding/json"
	"strings"
	"sync"
	"time"
)

const (
	Base429Cooldown = 5 * time.Second
	Max429Cooldown  = 60 * time.Second
)

var DefaultBreaker = NewBreaker429()

type Breaker429 struct {
	clock SleepClock

	mu          sync.Mutex
	tripped     bool
	tripAt      time.Time
	cooldown    time.Duration
	consecutive int
}

func NewBreaker429() *Breaker429 {
	return &Breaker429{clock: systemClock{}}
}

func (b *Breaker429) Trip() {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.consecutive++
	cd := Base429Cooldown << uint(b.consecutive-1)
	if cd > Max429Cooldown || cd <= 0 {
		cd = Max429Cooldown
	}
	b.cooldown = cd
	b.tripped = true
	b.tripAt = b.clock.Now()
}

func (b *Breaker429) WaitTripped(ctx context.Context) error {
	if ctx == nil {
		ctx = context.Background()
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if !b.tripped {
		return nil
	}
	remaining := b.cooldown - b.clock.Now().Sub(b.tripAt)
	if remaining <= 0 {
		return nil
	}
	return b.clock.Sleep(ctx, remaining)
}

func (b *Breaker429) Reset() {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.tripped {
		b.tripped = false
		b.consecutive = 0
	}
}

func (b *Breaker429) Tripped() bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.tripped
}

func (b *Breaker429) Cooldown() time.Duration {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.cooldown
}

type ExitBreakers struct {
	per      map[string]*Breaker429
	fallback *Breaker429
}

func NewExitBreakers(exitKeys []string, fallback *Breaker429) *ExitBreakers {
	per := make(map[string]*Breaker429, len(exitKeys))
	for _, k := range exitKeys {
		if k == "" {
			continue
		}
		if _, dup := per[k]; dup {
			continue
		}
		per[k] = NewBreaker429()
	}
	if fallback == nil {
		fallback = DefaultBreaker
	}
	return &ExitBreakers{per: per, fallback: fallback}
}

func (e *ExitBreakers) ForExit(key string) *Breaker429 {
	if len(e.per) == 0 {
		return e.fallback
	}
	if b, ok := e.per[key]; ok {
		return b
	}
	return e.fallback
}

func (e *ExitBreakers) ExitCount() int { return len(e.per) }

// Phantom429Payload 判定 429 响应体是否为可按成功处理的载荷。
type Phantom429Payload func(body []byte, appid string) (any, bool)

// AppDetailsPhantom429 仅认 {appid:{success:true,data:{...}}}（带 appid）或
// {data:{...}}（不带 appid），要求 data 为对象，避免误放行仅含 success 的真 429。
func AppDetailsPhantom429(body []byte, appid string) (any, bool) {
	if !strings.HasPrefix(strings.TrimSpace(string(body)), "{") {
		return nil, false
	}
	var data map[string]any
	if err := json.Unmarshal(body, &data); err != nil {
		return nil, false
	}
	if appid != "" {
		entry, ok := data[appid].(map[string]any)
		if !ok {
			return nil, false
		}
		if succ, _ := entry["success"].(bool); succ {
			if _, isObj := entry["data"].(map[string]any); isObj {
				return data, true
			}
		}
		return nil, false
	}
	if _, isObj := data["data"].(map[string]any); isObj {
		return data, true
	}
	return nil, false
}
