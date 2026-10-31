// Package netcheck：断网检测——把「本机没有网」与「代理/目标不可用」区分开，
// 两者处置相反：本机断网时换出口毫无意义（所有出口都不通），应停止消耗等
// 恢复；代理/目标不可用时本机网络是好的，应换出口重试。
//
// 判定不看单次请求结论，只看连续失败的形状：连续传输层失败达阈值后，绕开
// 全部代理直连探测本机网络，拿到任意响应（429/404 同样证明链路通）即否决
// 断网、把失败归因到代理或目标侧。探测必须不经代理也不读代理环境变量——
// 否则「代理挂了」会让探测一起失败，两种情形的处置正好做反。
package netcheck

import (
	"context"
	"net"
	"net/http"
	"sync"
	"time"
)

// 缺省参数（对齐现网判定口径）。
const (
	DefaultFailureThreshold = 3 // 连续传输失败达此次数 → 值得怀疑，转入核实
	DefaultPingAttempts     = 3 // 核实阶段的探测轮数（任一轮通即否决断网）
	DefaultPingTimeout      = 5 * time.Second
	DefaultPingRetryGap     = 2 * time.Second
	DefaultPollInterval     = 30 * time.Second
)

// DefaultPingURLs 直连可达的国内探测目标（可整表替换注入）。
var DefaultPingURLs = []string{"http://www.baidu.com", "https://www.qq.com"}

// Checker 网络状态判定器：只有「正常 / 已确认断网」两个状态，中间的失败计数
// 只是怀疑度——到线触发一次核实，本身不改变任何行为。可多 goroutine 并发上报。
type Checker struct {
	// 注入项：零值取缺省。PingURLs 指向本地测试桩时全程不触真实网络。
	PingURLs     []string
	Threshold    int
	PingAttempts int
	PingTimeout  time.Duration
	PingRetryGap time.Duration
	PollInterval time.Duration
	// Logf 判定与恢复事件出口（level ∈ info/warning/error）；nil 丢弃。
	Logf func(level, format string, args ...any)

	mu        sync.Mutex
	count     int
	offline   bool
	confirmMu sync.Mutex // 确认权单飞：同 Python _confirm_lock，核实期间并发上报在门外排队

	clientOnce sync.Once
	client     *http.Client
}

// IsOffline 是否已确认断网。
func (c *Checker) IsOffline() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.offline
}

// Reset 清零状态（网络恢复后复位；测试隔离用）。
func (c *Checker) Reset() {
	c.mu.Lock()
	c.count = 0
	c.offline = false
	c.mu.Unlock()
}

// ReportSuccess 一次真实 HTTP 交换完成——网络活着的直接证据，直接推翻断网判定
// 并清零怀疑计数。
func (c *Checker) ReportSuccess() {
	c.mu.Lock()
	c.count = 0
	c.offline = false
	c.mu.Unlock()
}

// ReportFailure 上报一次传输层失败（连接拒绝/超时/DNS 失败）。HTTP 层的 4xx/5xx
// 是「网络通、目标答了」，不计入。计数到线时原地核实（探测最长约
// 轮数×目标数×超时 + 轮间隔），期间阻塞调用方——同现网 await 语义；期间已有
// 其他 goroutine 完成核实或成功请求清零计数时直接返回。
func (c *Checker) ReportFailure() {
	shouldConfirm := false
	c.mu.Lock()
	if !c.offline { // 已确认断网期间不累计：计数没有消费者，只会在长断网里无限增长
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
		c.mu.Unlock() // 排队期间已被别人核实过，或计数已被成功请求清零
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

// WaitUntilOnline 等待本机网络恢复：每 PollInterval 直连探测一次。
// 返回 true=已恢复（状态清零，调用方可继续）；false=停止信号（ctx 结束）。
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

// probeWithRetries 最多探测 attempts 轮，任一轮通即返回 true。单轮失败不足以
// 定论（DNS 抖动、单个探测目标临时不可用），给抖动留几轮窗口再下结论。
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

// pingOnce 直连探测本机网络是否可达；任一目标给出响应即算通。
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
		return false // 这一个目标没通不代表本机断网，换下一个再试
	}
	_ = resp.Body.Close()
	return true // 拿到响应即证明链路可达——状态码无关紧要，429/404/503 都算通
}

// httpClient 探测客户端：单飞构建。传输层不设 Proxy——既不走配置代理，也
// 不读 HTTP_PROXY 等环境变量（显式 nil，非 http.ProxyFromEnvironment）。
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
