// 429 熔断：连续真 429 触发冷却，冷却期全部请求持锁等待，到期放行试探。
//
// 冷却时长从 5s 起步、按连续次数指数翻倍，60s 封顶；任一请求成功即复位。
// 冷却到期不自动复位——放行的请求就是试探：再遇真 429 则连续次数继续
// 累加、冷却继续翻倍，成功一次才归零。多出口形态每个出口 IP 各一个熔断器，
// 同出口的 worker 共享（同一 IP 的流量风控不按 worker 区分）。
package crawler

import (
	"context"
	"encoding/json"
	"strings"
	"sync"
	"time"
)

const (
	// Base429Cooldown：首次熔断的冷却时长。
	Base429Cooldown = 5 * time.Second
	// Max429Cooldown：连续熔断的冷却封顶。
	Max429Cooldown = 60 * time.Second
)

// DefaultBreaker：进程级单例熔断器，无出口键的调用方回落到它。
var DefaultBreaker = NewBreaker429()

// Breaker429：单份预算上的 429 熔断器。
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

// Trip：收到一次真 429。连续次数加一，冷却翻倍并封顶，冷却期从此刻起算。
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

// WaitTripped：冷却期内阻塞等满剩余冷却，到期放行试探；未熔断即取即发。
//
// 等待持锁进行：等待期间其它 goroutine 的 Trip/Reset 一律排队。等待不循环——
// 睡醒即放行，若试探再失败由调用方重新 Trip。
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

// Reset：一次成功请求后复位。连续次数与熔断标记一并清零，下次熔断从
// 基础冷却重新起步。
func (b *Breaker429) Reset() {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.tripped {
		b.tripped = false
		b.consecutive = 0
	}
}

// Tripped：当前是否处于熔断冷却期。
func (b *Breaker429) Tripped() bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.tripped
}

// Cooldown：当前冷却时长（观测口，供测试与诊断）。
func (b *Breaker429) Cooldown() time.Duration {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.cooldown
}

// ExitBreakers：按出口键分账的熔断账本。某出口被风控只闸住该出口，
// 其它出口继续工作；空/未知出口键回落进程级单例。
type ExitBreakers struct {
	per      map[string]*Breaker429
	fallback *Breaker429
}

// NewExitBreakers：每个非空出口键一个熔断器，同键重复只建一份。
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

// ForExit：取出口键对应的熔断器；无账本或未知键回落共享实例。
func (e *ExitBreakers) ForExit(key string) *Breaker429 {
	if len(e.per) == 0 {
		return e.fallback
	}
	if b, ok := e.per[key]; ok {
		return b
	}
	return e.fallback
}

// ExitCount：分账的出口数。
func (e *ExitBreakers) ExitCount() int { return len(e.per) }

// Phantom429Payload：幻觉 429 形状判定。入参为状态码 429 的响应体原文与
// 请求的 appid（可空）；判定该响应实为成功载荷时返回解析出的载荷与 true，
// 调用方据此直接按成功处理并复位熔断。判定失败的 429 一律按真 429 熔断。
type Phantom429Payload func(body []byte, appid string) (any, bool)

// AppDetailsPhantom429：appdetails 形状的幻觉 429 判定。
//
// 只认两种形状：带 appid 时 `{appid: {success: true, data: {...}}}`，不带
// appid 时 `{data: {...}}`。仅顶层 success 为真不足以判定——success 是
// 「本次请求处理成功」的通用标记，出现在任何 JSON API 的成功体里；一旦
// 调用方的成功体恰好带顶层 success，真 429 会被当成功返回且不再重试，
// 缺口静默固化，因此额外要求 data 是对象。其它形状（如 `{response: ...}`）
// 一律不匹配，按真 429 熔断。
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
