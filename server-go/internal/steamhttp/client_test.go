package steamhttp

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"holdexar/server-go/internal/crawler"
)

// fakeClock：Sleep 只登记时长不真睡，供退避序列断言。
type fakeClock struct {
	mu     sync.Mutex
	now    time.Time
	sleeps []time.Duration
}

func newFakeClock() *fakeClock { return &fakeClock{now: time.Unix(0, 0)} }

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *fakeClock) Sleep(_ context.Context, d time.Duration) error {
	c.mu.Lock()
	c.sleeps = append(c.sleeps, d)
	c.now = c.now.Add(d)
	c.mu.Unlock()
	return nil
}

func (c *fakeClock) recorded() []time.Duration {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]time.Duration(nil), c.sleeps...)
}

// recorder：收集出口账本回调。
type recorder struct {
	mu      sync.Mutex
	records []RequestRecord
}

func (r *recorder) add(rec RequestRecord) {
	r.mu.Lock()
	r.records = append(r.records, rec)
	r.mu.Unlock()
}

func (r *recorder) outcomes() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]string, 0, len(r.records))
	for _, rec := range r.records {
		out = append(out, rec.Outcome)
	}
	return out
}

// newTestClient：短退避 + 无抖动 + 模拟时钟 + 独立出口账本的测试客户端。
func newTestClient(t *testing.T, mutate func(*Config)) (*Client, *recorder) {
	t.Helper()
	rec := &recorder{}
	cfg := Config{
		Backoff429Base:   5 * time.Millisecond,
		BackoffOtherBase: 5 * time.Millisecond,
		JitterMax:        -1, // 无抖动，退避序列可精确断言
		RateLimits:       crawler.NewExitRateLimits([]string{"exit-a"}, 200, 300*time.Second, nil),
		Breakers:         crawler.NewExitBreakers([]string{"exit-a"}, nil),
		OnRequest:        rec.add,
	}
	if mutate != nil {
		mutate(&cfg)
	}
	c := New(cfg)
	c.clock = newFakeClock()
	return c, rec
}

func sameStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// jsonServer：命中计数 + 固定 JSON 响应。
type jsonServer struct {
	srv  *httptest.Server
	mu   sync.Mutex
	hits int
}

func newJSONServer(t *testing.T, body string) *jsonServer {
	t.Helper()
	js := &jsonServer{}
	js.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		js.mu.Lock()
		js.hits++
		js.mu.Unlock()
		fmt.Fprint(w, body)
	}))
	t.Cleanup(js.srv.Close)
	return js
}

func (js *jsonServer) count() int {
	js.mu.Lock()
	defer js.mu.Unlock()
	return js.hits
}

// ── 成功链 + 头部契约 + UA 轮换 ─────────────────────────────────

func TestGetJSONSuccessAndHeaderContract(t *testing.T) {
	var mu sync.Mutex
	var uas, conns []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		uas = append(uas, r.Header.Get("User-Agent"))
		conns = append(conns, r.Header.Get("Connection"))
		mu.Unlock()
		fmt.Fprint(w, `{"ok": true}`)
	}))
	defer srv.Close()

	c, rec := newTestClient(t, nil)
	for i := 0; i < len(userAgents); i++ {
		status, body, err := c.GetJSON(context.Background(), srv.URL, "exit-a")
		if err != nil || status != http.StatusOK {
			t.Fatalf("第 %d 次请求应成功，实际 status=%d err=%v", i, status, err)
		}
		if string(body) != `{"ok": true}` {
			t.Fatalf("响应体应原样返回，实际 %q", body)
		}
	}
	idx := func(ua string) int {
		for i, u := range userAgents {
			if u == ua {
				return i
			}
		}
		return -1
	}
	mu.Lock()
	defer mu.Unlock()
	if len(uas) != len(userAgents) {
		t.Fatalf("应发出 %d 发，实际 %d", len(userAgents), len(uas))
	}
	first := idx(uas[0])
	if first < 0 {
		t.Fatalf("UA 应来自池内: %q", uas[0])
	}
	for i, ua := range uas {
		if idx(ua) != (first+i)%len(userAgents) {
			t.Fatalf("UA 应按池顺序轮换，第 %d 个 %q 不符", i, ua)
		}
	}
	for i, conn := range conns {
		if conn != "close" {
			t.Fatalf("第 %d 发应带 Connection: close，实际 %q", i, conn)
		}
	}
	want := make([]string, len(userAgents))
	for i := range want {
		want[i] = OutcomeOK
	}
	if got := rec.outcomes(); !sameStrings(got, want) {
		t.Fatalf("每次真实请求都应记出口账本 ok，实际 %v", got)
	}
}

// ── 真 429：熔断 + 退避 + 成功复位 ──────────────────────────────

func TestReal429BackoffThenSuccess(t *testing.T) {
	js := &jsonServer{}
	js.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		js.mu.Lock()
		js.hits++
		n := js.hits
		js.mu.Unlock()
		if n == 1 {
			w.WriteHeader(http.StatusTooManyRequests)
			fmt.Fprint(w, "<html>429</html>")
			return
		}
		fmt.Fprint(w, `{"ok": true}`)
	}))
	defer js.srv.Close()

	c, rec := newTestClient(t, nil)
	fc := c.clock.(*fakeClock)
	start := time.Now()
	status, body, err := c.GetJSON(context.Background(), js.srv.URL, "exit-a")
	if err != nil || status != http.StatusOK || string(body) != `{"ok": true}` {
		t.Fatalf("重试后应成功，实际 status=%d err=%v", status, err)
	}
	// 客户端侧真 429 退避一次（模拟时钟登记，无抖动）
	if sleeps := fc.recorded(); len(sleeps) != 1 || sleeps[0] != 5*time.Millisecond {
		t.Fatalf("真 429 应退避一次 5ms，实际 %v", fc.recorded())
	}
	// 熔断冷却真实等待了一个冷却期（爬侧冷却 5s 起步，crawler 内部时钟不可注入）
	if elapsed := time.Since(start); elapsed < 4*time.Second {
		t.Fatalf("第二次尝试应阻塞在熔断冷却等待，实际 %v", elapsed)
	}
	if got := rec.outcomes(); !sameStrings(got, []string{OutcomeRateLimited, OutcomeOK}) {
		t.Fatalf("出口账本应记 [e429, ok]，实际 %v", got)
	}
	if js.count() != 2 {
		t.Fatalf("应共发两发，实际 %d", js.count())
	}
	if c.cfg.Breakers.ForExit("exit-a").Tripped() {
		t.Fatalf("成功后熔断应复位")
	}
}

// ── 幻觉 429 清洗 ───────────────────────────────────────────────

func TestPhantom429Cleaned(t *testing.T) {
	phantomBody := `{"730":{"success":true,"data":{"price_overview":{"final":1999}}}}`
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusTooManyRequests)
		fmt.Fprint(w, phantomBody)
	}))
	defer srv.Close()

	c, rec := newTestClient(t, nil)
	status, body, err := c.GetJSON(context.Background(), srv.URL, "exit-a", WithAppID("730"))
	if err != nil || status != http.StatusTooManyRequests {
		t.Fatalf("幻觉 429 应按成功清洗返回，实际 status=%d err=%v", status, err)
	}
	if string(body) != phantomBody {
		t.Fatalf("清洗成功应原样返回载荷，实际 %q", body)
	}
	if got := rec.outcomes(); !sameStrings(got, []string{OutcomeOK}) {
		t.Fatalf("清洗成功应记 ok，实际 %v", got)
	}
	if c.cfg.Breakers.ForExit("exit-a").Tripped() {
		t.Fatalf("幻觉 429 不应触发熔断")
	}
	if len(c.clock.(*fakeClock).recorded()) != 0 {
		t.Fatalf("清洗成功不应退避重试")
	}
}

func TestPhantom429BareShape(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusTooManyRequests)
		fmt.Fprint(w, `{"success": true, "data": {"x": 1}}`)
	}))
	defer srv.Close()

	c, _ := newTestClient(t, nil)
	status, _, err := c.GetJSON(context.Background(), srv.URL, "exit-a")
	if err != nil || status != http.StatusTooManyRequests {
		t.Fatalf("裸形状幻觉 429 应清洗成功，实际 status=%d err=%v", status, err)
	}
}

func TestReal429Exhausted(t *testing.T) {
	// StoreBrowse 形状两条分支都不匹配 → 真 429 照常熔断
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusTooManyRequests)
		fmt.Fprint(w, `{"response": {}}`)
	}))
	defer srv.Close()

	c, rec := newTestClient(t, func(cfg *Config) { cfg.MaxRetries = 1 })
	status, _, err := c.GetJSON(context.Background(), srv.URL, "exit-a")
	var rle *RateLimitError
	if !errors.As(err, &rle) {
		t.Fatalf("重试耗尽应返回限流错误，实际 %v", err)
	}
	if status != http.StatusTooManyRequests {
		t.Fatalf("status 应为 429，实际 %d", status)
	}
	if got := rec.outcomes(); !sameStrings(got, []string{OutcomeRateLimited}) {
		t.Fatalf("出口账本应记 e429，实际 %v", got)
	}
	if !c.cfg.Breakers.ForExit("exit-a").Tripped() {
		t.Fatalf("真 429 应触发本出口熔断")
	}
}

// ── 403/404 不重试 ──────────────────────────────────────────────

func Test403404NoRetry(t *testing.T) {
	for _, status := range []int{http.StatusForbidden, http.StatusNotFound} {
		t.Run(fmt.Sprint(status), func(t *testing.T) {
			js := &jsonServer{}
			js.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				js.mu.Lock()
				js.hits++
				js.mu.Unlock()
				w.WriteHeader(status)
				fmt.Fprint(w, "blocked")
			}))
			defer js.srv.Close()

			c, rec := newTestClient(t, func(cfg *Config) { cfg.MaxRetries = 3 })
			gotStatus, _, err := c.GetJSON(context.Background(), js.srv.URL, "exit-a")
			var se *StatusError
			if !errors.As(err, &se) || se.Status != status {
				t.Fatalf("应返回状态错误，实际 %v", err)
			}
			if gotStatus != status {
				t.Fatalf("status 应为 %d，实际 %d", status, gotStatus)
			}
			if js.count() != 1 {
				t.Fatalf("403/404 不重试，应只发一发，实际 %d", js.count())
			}
			if got := rec.outcomes(); !sameStrings(got, []string{OutcomeE4xx}) {
				t.Fatalf("出口账本应记 e4xx，实际 %v", got)
			}
			if len(c.clock.(*fakeClock).recorded()) != 0 {
				t.Fatalf("不重试路径不应有退避等待")
			}
		})
	}
}

// ── 建连失败：计数 + 换出口回调 ─────────────────────────────────

func TestConnectFailSwitchExit(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := ln.Addr().String()
	ln.Close() // 先起后关：拨号必被拒

	var mu sync.Mutex
	var exhausted []string
	c, rec := newTestClient(t, func(cfg *Config) {
		cfg.MaxRetries = 3
		cfg.OnExitExhausted = func(k string) {
			mu.Lock()
			exhausted = append(exhausted, k)
			mu.Unlock()
		}
	})
	deadURL := "http://" + addr + "/x"
	for i := 0; i < 3; i++ {
		status, body, reqErr := c.GetJSON(context.Background(), deadURL, "exit-a")
		if reqErr == nil || status != 0 || body != nil {
			t.Fatalf("第 %d 次应建连失败上抛，实际 status=%d err=%v", i, status, reqErr)
		}
	}
	if got := rec.outcomes(); !sameStrings(got, []string{OutcomeConnectErr, OutcomeConnectErr, OutcomeConnectErr}) {
		t.Fatalf("建连失败应记 connect_error，实际 %v", got)
	}
	if len(c.clock.(*fakeClock).recorded()) != 0 {
		t.Fatalf("建连失败不在同出口重试，不应有退避等待")
	}
	mu.Lock()
	if len(exhausted) != 1 || exhausted[0] != "exit-a" {
		t.Fatalf("跨过阈值应触发一次换出口回调，实际 %v", exhausted)
	}
	mu.Unlock()

	// 成功复位计数：之后一次失败不重复触发回调
	okSrv := newJSONServer(t, `{"ok": true}`)
	if _, _, err := c.GetJSON(context.Background(), okSrv.srv.URL, "exit-a"); err != nil {
		t.Fatal(err)
	}
	if _, _, err := c.GetJSON(context.Background(), deadURL, "exit-a"); err == nil {
		t.Fatal("应失败")
	}
	mu.Lock()
	defer mu.Unlock()
	if len(exhausted) != 1 {
		t.Fatalf("成功复位后一次失败不应再触发回调，实际 %v", exhausted)
	}
}

// ── 限流被真实消费 ──────────────────────────────────────────────

func TestRateLimiterConsumed(t *testing.T) {
	okSrv := newJSONServer(t, `{"ok": true}`)
	c, _ := newTestClient(t, func(cfg *Config) {
		cfg.RateLimits = crawler.NewExitRateLimits([]string{"exit-a"}, 1, 150*time.Millisecond, nil)
	})
	c.clock = systemClock{} // 真实时钟：窗口等待真实发生
	start := time.Now()
	for i := 0; i < 2; i++ {
		if _, _, err := c.GetJSON(context.Background(), okSrv.srv.URL, "exit-a"); err != nil {
			t.Fatal(err)
		}
	}
	if elapsed := time.Since(start); elapsed < 130*time.Millisecond {
		t.Fatalf("第二发应等待窗口出清（≥130ms），实际 %v", elapsed)
	}
}

// ── 熔断被真实消费（按出口分账） ────────────────────────────────

func TestBreakerConsumedPerExit(t *testing.T) {
	okSrv := newJSONServer(t, `{"ok": true}`)
	fallbackLimiter := crawler.NewRateLimiter(200, 300*time.Second)
	fallbackBreaker := crawler.NewBreaker429()
	c, _ := newTestClient(t, func(cfg *Config) {
		cfg.RateLimits = crawler.NewExitRateLimits([]string{"exit-a", "exit-b"}, 200, 300*time.Second, fallbackLimiter)
		cfg.Breakers = crawler.NewExitBreakers([]string{"exit-a", "exit-b"}, fallbackBreaker)
	})
	c.cfg.Breakers.ForExit("exit-a").Trip()

	// exit-a 在熔断冷却内被阻塞，取消后不发请求
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		_, _, err := c.GetJSON(ctx, okSrv.srv.URL, "exit-a")
		done <- err
	}()
	select {
	case err := <-done:
		t.Fatalf("冷却期内不应放行，实际 %v", err)
	case <-time.After(100 * time.Millisecond):
	}
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatalf("熔断等待应可取消，实际 %v", err)
	}
	if okSrv.count() != 0 {
		t.Fatalf("熔断等待期不应发出请求，实际 %d", okSrv.count())
	}
	// exit-b 不受 exit-a 熔断影响
	status, _, err := c.GetJSON(context.Background(), okSrv.srv.URL, "exit-b")
	if err != nil || status != http.StatusOK {
		t.Fatalf("exit-b 应不受影响正常发出，实际 status=%d err=%v", status, err)
	}
	if okSrv.count() != 1 {
		t.Fatalf("exit-b 应发出一发，实际 %d", okSrv.count())
	}
}

// ── 传输层缓存与 TLS 分档 ───────────────────────────────────────

func TestTransportCache(t *testing.T) {
	c, _ := newTestClient(t, func(cfg *Config) { cfg.Timeout = time.Second })
	a1, err := c.transportFor(true)
	if err != nil {
		t.Fatal(err)
	}
	a2, _ := c.transportFor(true)
	if a1 != a2 {
		t.Fatalf("同参应命中缓存复用同实例")
	}
	if b, _ := c.transportFor(false); a1 == b {
		t.Fatalf("校验档不同应换实例")
	}
	c2, _ := newTestClient(t, func(cfg *Config) { cfg.Timeout = 2 * time.Second })
	if b2, _ := c2.transportFor(true); a1 == b2 {
		t.Fatalf("超时档不同应换实例")
	}
	c3, _ := newTestClient(t, func(cfg *Config) { cfg.ProxyURL = "socks5://127.0.0.1:1080" })
	p1, err := c3.transportFor(true)
	if err != nil {
		t.Fatal(err)
	}
	if p1 == a1 {
		t.Fatalf("代理不同应换实例")
	}
	if p1.Proxy == nil {
		t.Fatalf("代理应作用于传输层")
	}
}

func TestTLSVerifyTiering(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"ok": true}`)
	}))
	defer srv.Close()

	c, rec := newTestClient(t, nil)
	// 缺省校验档：自签证书被拒，属建连类失败不重试
	status, _, err := c.GetJSON(context.Background(), srv.URL, "exit-a")
	if err == nil || status != 0 {
		t.Fatalf("缺省档应拒绝自签证书，实际 status=%d err=%v", status, err)
	}
	if got := rec.outcomes(); !sameStrings(got, []string{OutcomeConnectErr}) {
		t.Fatalf("TLS 握手失败应记 connect_error，实际 %v", got)
	}
	if len(c.clock.(*fakeClock).recorded()) != 0 {
		t.Fatalf("TLS 握手失败不应重试")
	}
	// 请求级放宽校验档：同参复用同一传输
	status, body, err := c.GetJSON(context.Background(), srv.URL, "exit-a", WithInsecureTLS(true))
	if err != nil || status != http.StatusOK || string(body) != `{"ok": true}` {
		t.Fatalf("放宽档应成功，实际 status=%d err=%v", status, err)
	}
}

// ── 502 隧道失败与普通 5xx ──────────────────────────────────────

func Test502TunnelFail(t *testing.T) {
	// 空 502 无 Content-Type：网关隧道失败，通道侧故障不重试
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
	}))
	defer srv.Close()

	var mu sync.Mutex
	var exhausted []string
	c, rec := newTestClient(t, func(cfg *Config) {
		cfg.MaxRetries = 3
		cfg.OnExitExhausted = func(k string) {
			mu.Lock()
			exhausted = append(exhausted, k)
			mu.Unlock()
		}
	})
	for i := 0; i < 3; i++ {
		status, _, err := c.GetJSON(context.Background(), srv.URL, "exit-a")
		var se *StatusError
		if !errors.As(err, &se) || se.Status != http.StatusBadGateway {
			t.Fatalf("应返回 502 状态错误，实际 %v", err)
		}
		if status != http.StatusBadGateway {
			t.Fatalf("status 应为 502，实际 %d", status)
		}
	}
	want := make([]string, 3)
	for i := range want {
		want[i] = OutcomeConnectErr
	}
	if got := rec.outcomes(); !sameStrings(got, want) {
		t.Fatalf("隧道失败应记 connect_error×3，实际 %v", got)
	}
	if len(c.clock.(*fakeClock).recorded()) != 0 {
		t.Fatalf("隧道失败不应重试")
	}
	mu.Lock()
	defer mu.Unlock()
	if len(exhausted) != 1 || exhausted[0] != "exit-a" {
		t.Fatalf("跨过阈值应触发一次换出口回调，实际 %v", exhausted)
	}
}

func Test502WithBodyRetries(t *testing.T) {
	// 带 502（有响应体）：普通 5xx 可重试
	js := &jsonServer{}
	js.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		js.mu.Lock()
		js.hits++
		js.mu.Unlock()
		w.WriteHeader(http.StatusBadGateway)
		fmt.Fprint(w, "bad gateway")
	}))
	defer js.srv.Close()

	c, rec := newTestClient(t, nil) // MaxRetries=3
	status, _, err := c.GetJSON(context.Background(), js.srv.URL, "exit-a")
	var se *StatusError
	if !errors.As(err, &se) || se.Status != http.StatusBadGateway {
		t.Fatalf("重试耗尽应返回 502 状态错误，实际 %v", err)
	}
	if status != http.StatusBadGateway {
		t.Fatalf("status 应为 502，实际 %d", status)
	}
	if js.count() != 3 {
		t.Fatalf("普通 5xx 应重试满 3 发，实际 %d", js.count())
	}
	want := []string{OutcomeE5xx, OutcomeE5xx, OutcomeE5xx}
	if got := rec.outcomes(); !sameStrings(got, want) {
		t.Fatalf("出口账本应记 e5xx×3，实际 %v", got)
	}
}

// ── 读超时 ──────────────────────────────────────────────────────

func TestTimeoutOutcome(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(200 * time.Millisecond)
		fmt.Fprint(w, `{"ok": true}`)
	}))
	defer srv.Close()

	c, rec := newTestClient(t, func(cfg *Config) {
		cfg.Timeout = 30 * time.Millisecond
		cfg.MaxRetries = 1
	})
	c.clock = systemClock{}
	status, _, err := c.GetJSON(context.Background(), srv.URL, "exit-a")
	if err == nil || status != 0 {
		t.Fatalf("读超时应上抛，实际 status=%d err=%v", status, err)
	}
	if got := rec.outcomes(); !sameStrings(got, []string{OutcomeTimeout}) {
		t.Fatalf("读超时应记 timeout，实际 %v", got)
	}
}

// ── 重定向 ──────────────────────────────────────────────────────

func TestFollowRedirects(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/a", func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "/b", http.StatusFound)
	})
	mux.HandleFunc("/b", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"ok": true}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	c, _ := newTestClient(t, nil)
	status, body, err := c.GetJSON(context.Background(), srv.URL+"/a", "exit-a")
	if err != nil || status != http.StatusOK || string(body) != `{"ok": true}` {
		t.Fatalf("缺省应跟随重定向，实际 status=%d err=%v", status, err)
	}

	c2, _ := newTestClient(t, func(cfg *Config) { cfg.DisableRedirects = true; cfg.MaxRetries = 1 })
	gotStatus, _, err := c2.GetJSON(context.Background(), srv.URL+"/a", "exit-a")
	var se *StatusError
	if !errors.As(err, &se) || se.Status != http.StatusFound {
		t.Fatalf("禁用重定向应返回 302 状态错误，实际 %v", err)
	}
	if gotStatus != http.StatusFound {
		t.Fatalf("status 应为 302，实际 %d", gotStatus)
	}
}

// ── 响应体上限 ──────────────────────────────────────────────────

func TestBodyLimit(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, strings.Repeat("a", 4096))
	}))
	defer srv.Close()

	c, rec := newTestClient(t, func(cfg *Config) { cfg.BodyLimit = 100; cfg.MaxRetries = 1 })
	status, _, err := c.GetJSON(context.Background(), srv.URL, "exit-a")
	if err == nil || !strings.Contains(err.Error(), "超出上限") {
		t.Fatalf("超限应报上限错误，实际 %v", err)
	}
	if status != http.StatusOK {
		t.Fatalf("status 应保留 200，实际 %d", status)
	}
	if got := rec.outcomes(); !sameStrings(got, []string{OutcomeOther}) {
		t.Fatalf("超限应记 other，实际 %v", got)
	}
}

// ── 缺省装配 ────────────────────────────────────────────────────

func TestNewDefaults(t *testing.T) {
	c := New(Config{})
	if c.cfg.Timeout != DefaultTimeout || c.cfg.MaxRetries != DefaultMaxRetries ||
		c.cfg.BodyLimit != DefaultBodyLimit || c.cfg.ConnectFailThreshold != DefaultConnectFailThreshold {
		t.Fatalf("缺省项应对齐既有链：timeout=%v retries=%d limit=%d threshold=%d",
			c.cfg.Timeout, c.cfg.MaxRetries, c.cfg.BodyLimit, c.cfg.ConnectFailThreshold)
	}
	if c.cfg.Backoff429Base != 3*time.Second || c.cfg.BackoffOtherBase != time.Second || c.cfg.JitterMax != time.Second {
		t.Fatalf("缺省退避应对齐既有链")
	}
	if c.limiterFor("x") != crawler.DefaultRateLimiter || c.breakerFor("x") != crawler.DefaultBreaker {
		t.Fatalf("未注入账本应回落进程级单例")
	}
	// 幻觉判定行为对齐 crawler.AppDetailsPhantom429
	if _, ok := c.phantom([]byte(`{"success":true,"data":{}}`), ""); !ok {
		t.Fatalf("裸形状应判定为幻觉 429")
	}
	if _, ok := c.phantom([]byte(`{"response":{}}`), ""); ok {
		t.Fatalf("StoreBrowse 形状不应判定为幻觉 429")
	}
}
