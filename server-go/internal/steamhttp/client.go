// Steam HTTP 客户端：按出口限流 + 429 熔断 + 指数退避 + 幻觉 429 清洗。
//
// 限流与熔断组合 internal/crawler 的按出口原语（ExitRateLimits / ExitBreakers），
// 按调用传入的出口键取用；每次真实发出的 HTTP 请求（含重试）经 OnRequest 回调
// 记出口账本；连续建连失败达阈值触发 OnExitExhausted，由调用方换出口重推。
package steamhttp

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/rand/v2"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"holdexar/server-go/internal/crawler"
)

// UA 池：请求间轮换，避免同一出口长期携带单一指纹。
var userAgents = [...]string{
	"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
	"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
	"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
	"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
}

// 出口账本结果枚举（固定口径）。
const (
	OutcomeOK          = "ok"
	OutcomeRateLimited = "e429"
	OutcomeE4xx        = "e4xx"
	OutcomeE5xx        = "e5xx"
	OutcomeTimeout     = "timeout"
	OutcomeConnectErr  = "connect_error"
	OutcomeOther       = "other"
)

// 缺省参数（对齐既有爬取链）。
const (
	DefaultTimeout              = 12 * time.Second
	DefaultMaxRetries           = 3
	DefaultBodyLimit            = 64 << 20
	DefaultConnectFailThreshold = 3
)

// RequestRecord：一次真实发出的 HTTP 请求的出口账本事实。ExitKey 为空即直连。
type RequestRecord struct {
	ExitKey    string
	URL        string
	Outcome    string
	DurationMS int64
}

// RateLimitError：真 429 重试耗尽。
type RateLimitError struct{ Attempts int }

func (e *RateLimitError) Error() string {
	return fmt.Sprintf("上游限流：连续 %d 次真实 429，重试耗尽", e.Attempts)
}

// StatusError：非成功状态码的终态（403/404 直返、其余状态码重试耗尽）。
type StatusError struct {
	Status int
	Body   []byte
}

func (e *StatusError) Error() string { return fmt.Sprintf("HTTP %d", e.Status) }

// Config：客户端参数与注入项；零值取缺省，负数语义逐字段标注。
type Config struct {
	Timeout          time.Duration // 单请求总超时（含连接与读体），缺省 12s
	MaxRetries       int           // 总尝试次数，缺省 3
	ProxyURL         string        // 调试通道显式代理，空=直连（不读环境变量代理）
	InsecureTLS      bool          // TLS 校验缺省档；true=跳过校验（自签加速器场景）
	DisableRedirects bool          // true=不跟随重定向，3xx 按普通状态码处理
	BodyLimit        int64         // 响应体字节上限，缺省 64MiB；负数=不限

	Backoff429Base   time.Duration // 真 429 退避基数，按 2^n 递增，缺省 3s
	BackoffOtherBase time.Duration // 其余可重试失败的退避基数，缺省 1s
	JitterMax        time.Duration // 退避随机抖动上限，缺省 1s；负数=无抖动

	ConnectFailThreshold int // 连续建连失败换出口阈值，缺省 3

	RateLimits      *crawler.ExitRateLimits   // 按出口限流账本；nil 回落进程级单例
	Breakers        *crawler.ExitBreakers     // 按出口熔断账本；nil 回落进程级单例
	Phantom         crawler.Phantom429Payload // 幻觉 429 判定；nil 用 appdetails 形状
	OnRequest       func(RequestRecord)       // 每次真实请求（含重试）记出口账本
	OnExitExhausted func(exitKey string)      // 连续建连失败达阈值，提示换出口
}

// Client：无状态请求链 + 按出口的失败计数与传输层缓存，可多 goroutine 共享。
type Client struct {
	cfg        Config
	phantom    crawler.Phantom429Payload
	limiterFor func(string) *crawler.RateLimiter
	breakerFor func(string) *crawler.Breaker429
	clock      crawler.SleepClock
	uaIdx      atomic.Uint32

	mu          sync.Mutex
	transports  map[transportKey]*http.Transport
	consecFails map[string]int
}

// New：装配客户端并补齐缺省项。
func New(cfg Config) *Client {
	if cfg.Timeout <= 0 {
		cfg.Timeout = DefaultTimeout
	}
	if cfg.MaxRetries <= 0 {
		cfg.MaxRetries = DefaultMaxRetries
	}
	if cfg.BodyLimit == 0 {
		cfg.BodyLimit = DefaultBodyLimit
	}
	if cfg.Backoff429Base <= 0 {
		cfg.Backoff429Base = 3 * time.Second
	}
	if cfg.BackoffOtherBase <= 0 {
		cfg.BackoffOtherBase = 1 * time.Second
	}
	if cfg.JitterMax == 0 {
		cfg.JitterMax = 1 * time.Second
	} else if cfg.JitterMax < 0 {
		cfg.JitterMax = 0 // 负数=无抖动
	}
	if cfg.ConnectFailThreshold <= 0 {
		cfg.ConnectFailThreshold = DefaultConnectFailThreshold
	}
	phantom := cfg.Phantom
	if phantom == nil {
		phantom = crawler.AppDetailsPhantom429
	}
	limiterFor := func(string) *crawler.RateLimiter { return crawler.DefaultRateLimiter }
	if cfg.RateLimits != nil {
		limiterFor = cfg.RateLimits.ForExit
	}
	breakerFor := func(string) *crawler.Breaker429 { return crawler.DefaultBreaker }
	if cfg.Breakers != nil {
		breakerFor = cfg.Breakers.ForExit
	}
	c := &Client{
		cfg:         cfg,
		phantom:     phantom,
		limiterFor:  limiterFor,
		breakerFor:  breakerFor,
		clock:       systemClock{},
		transports:  make(map[transportKey]*http.Transport),
		consecFails: make(map[string]int),
	}
	c.uaIdx.Store(uint32(rand.IntN(len(userAgents))))
	return c
}

type requestOpts struct {
	appid       string
	insecureTLS *bool
}

type RequestOption func(*requestOpts)

// WithAppID：幻觉 429 按 appdetails 的 {appid:{success,data}} 形状判定；
// 缺省按裸 {success,data} 形状判定。
func WithAppID(appid string) RequestOption {
	return func(o *requestOpts) { o.appid = appid }
}

// WithInsecureTLS：本请求覆盖 TLS 校验档（自签加速器场景按站点分档）。
func WithInsecureTLS(insecure bool) RequestOption {
	return func(o *requestOpts) { o.insecureTLS = &insecure }
}

// GetJSON：GET 一个 JSON 接口。每次尝试先取本出口限流名额、再等本出口熔断冷却。
//
// status 是上游真实状态码；err 为 nil 时 body 即可用载荷——含「状态码 429 但
// body 是 appdetails 成功形状」的幻觉清洗形态。重试口径：真 429 按 2^n 退避；
// 其余可重试失败（5xx/非 JSON/读超时等）固定基数退避；403/404 不重试；建连类
// 失败不在同出口重试，立即上抛交由调用方换出口。
func (c *Client) GetJSON(ctx context.Context, urlStr, exitKey string, opts ...RequestOption) (int, []byte, error) {
	var o requestOpts
	for _, f := range opts {
		f(&o)
	}
	lim := c.limiterFor(exitKey)
	br := c.breakerFor(exitKey)
	for attempt := 0; attempt < c.cfg.MaxRetries; attempt++ {
		canRetry := attempt < c.cfg.MaxRetries-1
		backoffOther := func() error {
			return c.sleepBackoff(ctx, c.cfg.BackoffOtherBase, attempt, false)
		}
		if err := lim.Acquire(ctx); err != nil {
			return 0, nil, err
		}
		if err := br.WaitTripped(ctx); err != nil {
			return 0, nil, err
		}
		started := c.clock.Now()
		status, body, contentType, overLimit, err := c.send(ctx, urlStr, o)
		if err != nil {
			if ctx.Err() != nil {
				return 0, nil, ctx.Err() // 调用方取消：不发出口账，也不重试
			}
			outcome, connectClass := classifyTransportError(err)
			c.record(exitKey, urlStr, outcome, started)
			if connectClass {
				c.bumpConnectFail(exitKey)
				return 0, nil, err
			}
			if canRetry {
				if sleepErr := backoffOther(); sleepErr != nil {
					return 0, nil, sleepErr
				}
				continue
			}
			return 0, nil, err
		}
		if overLimit {
			c.record(exitKey, urlStr, OutcomeOther, started)
			if canRetry {
				if sleepErr := backoffOther(); sleepErr != nil {
					return 0, nil, sleepErr
				}
				continue
			}
			return status, nil, errors.New("响应体超出上限")
		}
		switch {
		case status == http.StatusOK && json.Valid(body):
			br.Reset()
			c.record(exitKey, urlStr, OutcomeOK, started)
			return status, body, nil

		case status == http.StatusOK:
			// 200 但非 JSON（HTML 错误页等）
			c.record(exitKey, urlStr, OutcomeOther, started)
			if canRetry {
				if sleepErr := backoffOther(); sleepErr != nil {
					return 0, nil, sleepErr
				}
				continue
			}
			return status, body, errors.New("响应不是有效 JSON")

		case status == http.StatusTooManyRequests:
			if _, ok := c.phantom(body, o.appid); ok {
				br.Reset()
				c.record(exitKey, urlStr, OutcomeOK, started)
				return status, body, nil
			}
			// 真 429：限流是主闸，熔断兜底
			br.Trip()
			c.record(exitKey, urlStr, OutcomeRateLimited, started)
			if canRetry {
				if sleepErr := c.sleepBackoff(ctx, c.cfg.Backoff429Base, attempt, true); sleepErr != nil {
					return 0, nil, sleepErr
				}
				continue
			}
			return status, body, &RateLimitError{Attempts: c.cfg.MaxRetries}

		case status == http.StatusForbidden || status == http.StatusNotFound:
			// 永久性封锁，不重试
			c.record(exitKey, urlStr, OutcomeE4xx, started)
			return status, body, &StatusError{Status: status, Body: body}

		case status == http.StatusBadGateway && len(bytes.TrimSpace(body)) == 0 && contentType == "":
			// 网关隧道失败（空 502 无 Content-Type）：通道侧故障
			c.record(exitKey, urlStr, OutcomeConnectErr, started)
			c.bumpConnectFail(exitKey)
			return status, body, &StatusError{Status: status, Body: body}

		default:
			outcome := OutcomeE4xx
			if status >= 500 {
				outcome = OutcomeE5xx
			}
			c.record(exitKey, urlStr, outcome, started)
			if canRetry {
				if sleepErr := backoffOther(); sleepErr != nil {
					return 0, nil, sleepErr
				}
				continue
			}
			return status, body, &StatusError{Status: status, Body: body}
		}
	}
	return 0, nil, errors.New("重试次数耗尽")
}

// send：发一次真实请求并读体。逐发轮换 UA、带 Connection: close（匀速限流下
// 不留 keep-alive）；传输层只缓存连接，客户端不挂 Cookie jar，响应 Cookie 不留存。
func (c *Client) send(ctx context.Context, urlStr string, o requestOpts) (status int, body []byte, contentType string, overLimit bool, err error) {
	verify := !c.cfg.InsecureTLS
	if o.insecureTLS != nil {
		verify = !*o.insecureTLS
	}
	tr, err := c.transportFor(verify)
	if err != nil {
		return 0, nil, "", false, err
	}
	hc := &http.Client{Transport: tr, Timeout: c.cfg.Timeout}
	if c.cfg.DisableRedirects {
		hc.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, urlStr, nil)
	if err != nil {
		return 0, nil, "", false, err
	}
	req.Header.Set("Accept-Language", "en-US,en;q=0.9")
	req.Header.Set("User-Agent", c.nextUA())
	req.Header.Set("Connection", "close")
	resp, err := hc.Do(req)
	if err != nil {
		return 0, nil, "", false, err
	}
	defer resp.Body.Close()
	raw := io.Reader(resp.Body)
	if c.cfg.BodyLimit > 0 {
		raw = io.LimitReader(resp.Body, c.cfg.BodyLimit+1)
	}
	body, err = io.ReadAll(raw)
	if err != nil {
		return 0, nil, "", false, err
	}
	contentType = resp.Header.Get("Content-Type")
	overLimit = c.cfg.BodyLimit > 0 && int64(len(body)) > c.cfg.BodyLimit
	return resp.StatusCode, body, contentType, overLimit, nil
}

func (c *Client) nextUA() string {
	i := int(c.uaIdx.Add(1)-1) % len(userAgents)
	return userAgents[i]
}

// record：把一次真实发出的请求记出口账本；成功同时复位该出口的连续建连失败计数。
func (c *Client) record(exitKey, urlStr, outcome string, started time.Time) {
	if outcome == OutcomeOK {
		c.mu.Lock()
		c.consecFails[exitKey] = 0
		c.mu.Unlock()
	}
	if c.cfg.OnRequest == nil {
		return
	}
	d := c.clock.Now().Sub(started)
	if d < 0 {
		d = 0
	}
	c.cfg.OnRequest(RequestRecord{
		ExitKey:    exitKey,
		URL:        urlStr,
		Outcome:    outcome,
		DurationMS: int64(d / time.Millisecond),
	})
}

// bumpConnectFail：建连类失败计数 +1；跨过阈值那一刻触发换出口回调（只触发一次）。
func (c *Client) bumpConnectFail(exitKey string) {
	c.mu.Lock()
	c.consecFails[exitKey]++
	n := c.consecFails[exitKey]
	c.mu.Unlock()
	if n == c.cfg.ConnectFailThreshold && c.cfg.OnExitExhausted != nil {
		c.cfg.OnExitExhausted(exitKey)
	}
}

// sleepBackoff：真 429 基数按 2^n 递增，其余失败固定基数；都叠加抖动。
func (c *Client) sleepBackoff(ctx context.Context, base time.Duration, attempt int, exponential bool) error {
	d := base
	if exponential {
		d = base << uint(attempt)
	}
	if c.cfg.JitterMax > 0 {
		d += time.Duration(rand.Int64N(int64(c.cfg.JitterMax)))
	}
	return c.clock.Sleep(ctx, d)
}

// systemClock：真实时钟（测试可整体注入替换）。
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
