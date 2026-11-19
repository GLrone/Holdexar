// Package netcheck 连续传输失败达阈值后绕开代理直连探测，区分本机断网与代理/目标故障。
package netcheck

import (
	"context"
	"net"
	"net/http"
	"sync"
	"time"
)

const (
	DefaultFailureThreshold = 3
	DefaultPingAttempts     = 3
	DefaultPingTimeout      = 5 * time.Second
	DefaultPingRetryGap     = 2 * time.Second
	DefaultPollInterval     = 30 * time.Second
)

var DefaultPingURLs = []string{"http://www.baidu.com", "https://www.qq.com"}

type Checker struct {
	PingURLs     []string
	Threshold    int
	PingAttempts int
	PingTimeout  time.Duration
	PingRetryGap time.Duration
	PollInterval time.Duration
	Logf         func(level, format string, args ...any)

	mu        sync.Mutex
	count     int
	offline   bool
	confirmMu sync.Mutex // 确认单飞锁：核实期间并发上报排队

	clientOnce sync.Once
	client     *http.Client
}

func (c *Checker) IsOffline() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.offline
}

func (c *Checker) Reset() {
	c.mu.Lock()
	c.count = 0
	c.offline = false
	c.mu.Unlock()
}

func (c *Checker) ReportSuccess() {
	c.mu.Lock()
	c.count = 0
	c.offline = false
	c.mu.Unlock()
}

// ReportFailure 仅计传输失败（HTTP 4xx/5xx 不计入），达阈值阻塞核实。
func (c *Checker) ReportFailure() {
	shouldConfirm := false
	c.mu.Lock()
	if !c.offline {
		c.count++
		if c.count >= c.threshold() {
			shouldConfirm = true
		}
	}
	c.mu.Unlock()
	if shouldConfirm {
		c.confirmOffline()
	}
}

func (c *Checker) confirmOffline() {
	c.confirmMu.Lock()
	defer c.confirmMu.Unlock()
	c.mu.Lock()
	if c.offline || c.count < c.threshold() {
		c.mu.Unlock()
		return
	}
	count := c.count
	c.mu.Unlock()
	if c.probeWithRetries(context.Background()) {
		c.mu.Lock()
		c.count = 0
		c.mu.Unlock()
		c.logf("info", "连续 %d 次传输失败，但本机可以直连外网，判定为代理或目标侧问题，转为换出口重试", count)
		return
	}
	c.mu.Lock()
	c.offline = true
	c.mu.Unlock()
	c.logf("error", "本机网络不可达（连续 %d 次传输失败，%d 轮直连探测全败），暂停抓取，等网络恢复后自动继续", count, c.attempts())
}

func (c *Checker) WaitUntilOnline(ctx context.Context) bool {
	c.logf("warning", "本机网络不可达，每 %d 秒探测一次，恢复后自动继续抓取", int(c.poll()/time.Second))
	for ctx.Err() == nil {
		if c.pingOnce(ctx) {
			c.Reset()
			c.logf("info", "本机网络已恢复，继续抓取")
			return true
		}
		if sleepCtx(ctx, c.poll()) {
			return false
		}
	}
	return false
}

func (c *Checker) probeWithRetries(ctx context.Context) bool {
	for attempt := 0; attempt < c.attempts(); attempt++ {
		if c.pingOnce(ctx) {
			return true
		}
		if attempt < c.attempts()-1 {
			if sleepCtx(ctx, c.gap()) {
				return false
			}
		}
	}
	return false
}

func (c *Checker) pingOnce(ctx context.Context) bool {
	for _, url := range c.urls() {
		if c.pingTarget(ctx, url) {
			return true
		}
	}
	return false
}

func (c *Checker) pingTarget(ctx context.Context, url string) bool {
	resp, err := c.httpClient().Get(url)
	if err != nil {
		return false
	}
	_ = resp.Body.Close()
	return true
}

// httpClient 显式不设 Proxy（既不走配置代理也不读代理环境变量）。
func (c *Checker) httpClient() *http.Client {
	c.clientOnce.Do(func() {
		timeout := c.pingTimeout()
		c.client = &http.Client{
			Transport: &http.Transport{
				DialContext:         (&net.Dialer{Timeout: timeout}).DialContext,
				TLSHandshakeTimeout: timeout,
				IdleConnTimeout:     90 * time.Second,
			},
			Timeout: timeout,
		}
	})
	return c.client
}

func sleepCtx(ctx context.Context, d time.Duration) bool {
	if d <= 0 {
		return ctx.Err() != nil
	}
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return true
	case <-timer.C:
		return false
	}
}

func (c *Checker) urls() []string {
	if len(c.PingURLs) > 0 {
		return c.PingURLs
	}
	return DefaultPingURLs
}

func (c *Checker) threshold() int {
	if c.Threshold > 0 {
		return c.Threshold
	}
	return DefaultFailureThreshold
}

func (c *Checker) attempts() int {
	if c.PingAttempts > 0 {
		return c.PingAttempts
	}
	return DefaultPingAttempts
}

func (c *Checker) pingTimeout() time.Duration {
	if c.PingTimeout > 0 {
		return c.PingTimeout
	}
	return DefaultPingTimeout
}

func (c *Checker) gap() time.Duration {
	if c.PingRetryGap > 0 {
		return c.PingRetryGap
	}
	return DefaultPingRetryGap
}

func (c *Checker) poll() time.Duration {
	if c.PollInterval > 0 {
		return c.PollInterval
	}
	return DefaultPollInterval
}

func (c *Checker) logf(level, format string, args ...any) {
	if c.Logf != nil {
		c.Logf(level, format, args...)
	}
}
