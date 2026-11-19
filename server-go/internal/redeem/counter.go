package redeem

import (
	"sync"
	"time"
)

const (
	ACTLimit         = 10
	ACTWindowSeconds = 30 * 60
)

// counter 按账号分桶的进程内滑动窗口（10 次/30 分钟，非持久化，仅作前端节奏提示）。
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

func (c *counter) record(steamID string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.log[steamID] = append(c.log[steamID], c.now())
}
