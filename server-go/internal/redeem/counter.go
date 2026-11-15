package redeem

import (
	"sync"
	"time"
)

// Steam 官方激活限制：30 分钟窗口内 10 次（无论成败）。
const (
	ACTLimit         = 10
	ACTWindowSeconds = 30 * 60
)

// counter 按账号分桶的进程内滑动窗口：Steam 的 10 次/30 分钟限制作用于单个
// 账号，换绑 Cookie 即换号重计。非持久化，重启清零——本端计数仅作前端节奏
// 指引，Steam 端才是真裁判。
type counter struct {
	mu     sync.Mutex
	log    map[string][]time.Time
	window time.Duration
	now    func() time.Time
}

func newCounter(window time.Duration, now func() time.Time) *counter {
	if now == nil {
		now = time.Now
	}
	return &counter{log: map[string][]time.Time{}, window: window, now: now}
}

// usage 该账号当前窗口内已用激活次数（过期时间戳顺手清理）。
func (c *counter) usage(steamID string) int {
	c.mu.Lock()
	defer c.mu.Unlock()
	now := c.now()
	stamps := c.log[steamID]
	kept := stamps[:0]
	for _, t := range stamps {
		if now.Sub(t) < c.window {
			kept = append(kept, t)
		}
	}
	if len(kept) == 0 {
		delete(c.log, steamID)
		return 0
	}
	c.log[steamID] = kept
	return len(kept)
}

// record 记一次激活尝试（无论成败，Steam 端同款语义）。
func (c *counter) record(steamID string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.log[steamID] = append(c.log[steamID], c.now())
}
