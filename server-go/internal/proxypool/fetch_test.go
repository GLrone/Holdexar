package proxypool

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func allowLocalDirectForTest(t *testing.T) {
	t.Helper()
	defaultDirectDialer = (&net.Dialer{Timeout: 8 * time.Second}).DialContext
	t.Cleanup(func() { defaultDirectDialer = nil })
}

const validSubscriptionBody = "proxies:\n  - {name: 测试节点, type: ss, server: a.example.com, port: 443, cipher: aes-256-gcm, password: p}\n"

func muxServer(t *testing.T, handler http.HandlerFunc) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	return srv
}

func slowBody(t *testing.T, delay time.Duration, body string, status int) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-time.After(delay):
			w.WriteHeader(status)
			_, _ = w.Write([]byte(body))
		case <-r.Context().Done():
		}
	}
}

func TestFetchHedgeWinner(t *testing.T) {
	fast := muxServer(t, slowBody(t, 30*time.Millisecond, validSubscriptionBody, 200))
	channels := []Channel{
		{Name: ChannelDirect, EgressID: ChannelDirect},
		{Name: ChannelKernel, Proxy: fast.URL, EgressID: "runtime"},
	}
	res, err := FetchSubscription(context.Background(), "http://sub.example.com/s", channels, FetchOptions{
		HedgeDelay: 0.02,
	})
	if err != nil {
		t.Fatalf("竞速应成功：%v", err)
	}
	if res.Channel != ChannelKernel {
		t.Fatalf("获胜通道应为快的内核通道，got %s", res.Channel)
	}
	if len(res.ResolvedNodes) != 1 || res.ResolvedNodes[0]["name"] != "测试节点" {
		t.Fatalf("获胜内容应有效：%v", res.ResolvedNodes)
	}
	if len(res.Attempts) == 0 || !res.Attempts[len(res.Attempts)-1].OK {
		t.Fatalf("获胜尝试应留痕且成功：%+v", res.Attempts)
	}
}

func TestFetchInvalidFastDoesNotWin(t *testing.T) {
	slow := muxServer(t, slowBody(t, 120*time.Millisecond, validSubscriptionBody, 200))
	fast := muxServer(t, slowBody(t, 20*time.Millisecond, "<html>劫持页</html>", 200))
	channels := []Channel{
		{Name: ChannelDirect, Proxy: fast.URL, EgressID: "direct"},
		{Name: ChannelKernel, Proxy: slow.URL, EgressID: "runtime"},
	}
	res, err := FetchSubscription(context.Background(), "http://sub.example.com/s", channels, FetchOptions{
		HedgeDelay: 0.02,
	})
	if err != nil {
		t.Fatalf("应有通道最终获胜：%v", err)
	}
	if res.Channel != ChannelKernel {
		t.Fatalf("无效内容不得获胜，获胜应为慢的有效通道，got %s", res.Channel)
	}
	if len(res.Attempts) != 2 || res.Attempts[0].OK || !res.Attempts[1].OK {
		t.Fatalf("尝试留痕不符：%+v", res.Attempts)
	}
}

func TestFetchParallelismCap(t *testing.T) {
	var inflight, peak atomic.Int64
	release := make(chan struct{})
	srv := muxServer(t, func(w http.ResponseWriter, r *http.Request) {
		cur := inflight.Add(1)
		for {
			old := peak.Load()
			if cur <= old || peak.CompareAndSwap(old, cur) {
				break
			}
		}
		<-release
		inflight.Add(-1)
	})
	channels := []Channel{
		{Name: "a", Proxy: srv.URL, EgressID: "a"},
		{Name: "b", Proxy: srv.URL, EgressID: "b"},
		{Name: "c", Proxy: srv.URL, EgressID: "c"},
		{Name: "d", Proxy: srv.URL, EgressID: "d"},
	}
	done := make(chan struct{})
	go func() {
		defer close(done)
		_, _ = FetchSubscription(context.Background(), "http://sub.example.com/s", channels, FetchOptions{
			HedgeDelay:   0.01,
			TotalTimeout: 3,
		})
	}()
	deadline := time.After(2 * time.Second)
	for peak.Load() < DefaultFetchParallelism {
		select {
		case <-done:
			t.Fatal("竞速提前结束，未达并发档")
		case <-deadline:
			t.Fatalf("并发峰值未达 %d", DefaultFetchParallelism)
		case <-time.After(10 * time.Millisecond):
		}
	}
	close(release)
	<-done
	if got := peak.Load(); got > DefaultFetchParallelism {
		t.Fatalf("并发峰值 %d 超上限 %d", got, DefaultFetchParallelism)
	}
}

func TestFetchDeadline(t *testing.T) {
	release := make(chan struct{})
	defer close(release)
	srv := muxServer(t, func(w http.ResponseWriter, r *http.Request) {
		<-r.Context().Done()
	})
	channels := []Channel{
		{Name: "a", Proxy: srv.URL, EgressID: "a"},
		{Name: "b", Proxy: srv.URL, EgressID: "b"},
	}
	started := time.Now()
	_, err := FetchSubscription(context.Background(), "http://sub.example.com/s", channels, FetchOptions{
		TotalTimeout: 0.15,
		HedgeDelay:   0.02,
	})
	if err == nil {
		t.Fatal("全部挂起应失败")
	}
	var ffe *FetchFailedError
	if !errors.As(err, &ffe) {
		t.Fatalf("应 FetchFailedError，got %T %v", err, err)
	}
	if time.Since(started) > 3*time.Second {
		t.Fatal("deadline 未生效，等待远超预算")
	}
	hasDeadlineMark := false
	for _, a := range ffe.Attempts {
		if a.Error == "subscription_deadline" && a.Stage == "overall_timeout" {
			hasDeadlineMark = true
		}
	}
	if !hasDeadlineMark {
		t.Fatalf("在飞通道应留 subscription_deadline 痕：%+v", ffe.Attempts)
	}
}

func TestFetchValidationChain(t *testing.T) {
	cases := []struct {
		name   string
		status int
		body   string
		want   string
	}{
		{"非200", 503, validSubscriptionBody, "*proxypool.FetchFailedError"},
		{"空体", 200, "", "*proxypool.EmptyNodeSetError"},
		{"HTML劫持", 200, "<html>blocked</html>", "*proxypool.UnsupportedFormatError"},
		{"JSON", 200, `{"proxies": []}`, "*proxypool.UnsupportedFormatError"},
	}
	for _, c := range cases {
		srv := muxServer(t, func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(c.status)
			_, _ = w.Write([]byte(c.body))
		})
		_, err := FetchSubscription(context.Background(), "http://sub.example.com/s",
			[]Channel{{Name: "only", Proxy: srv.URL, EgressID: "only"}}, FetchOptions{})
		if err == nil {
			t.Fatalf("%s：应失败", c.name)
		}
		if fmt.Sprintf("%T", err) != c.want {
			t.Fatalf("%s：期望 %s，got %T %v", c.name, c.want, err, err)
		}
	}
	var gotUA string
	srv := muxServer(t, func(w http.ResponseWriter, r *http.Request) {
		gotUA = r.Header.Get("User-Agent")
		w.WriteHeader(200)
		_, _ = w.Write([]byte(validSubscriptionBody))
	})
	if _, err := FetchSubscription(context.Background(), "http://sub.example.com/s",
		[]Channel{{Name: "only", Proxy: srv.URL, EgressID: "only"}}, FetchOptions{}); err != nil {
		t.Fatalf("有效订阅应成功：%v", err)
	}
	if gotUA != SUB_USER_AGENT {
		t.Fatalf("UA 应为 %q，got %q", SUB_USER_AGENT, gotUA)
	}
}

func TestEgressPreference(t *testing.T) {
	var mu sync.Mutex
	var order []string
	srv := muxServer(t, func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		order = append(order, r.Host)
		mu.Unlock()
		time.Sleep(20 * time.Millisecond)
		w.WriteHeader(200)
		_, _ = w.Write([]byte(validSubscriptionBody))
	})
	channels := []Channel{
		{Name: "x", Proxy: srv.URL, EgressID: "pool:1"},
		{Name: "y", Proxy: srv.URL, EgressID: "runtime"},
	}
	if _, err := FetchSubscription(context.Background(), "http://sub.example.com/s", channels, FetchOptions{
		PreferredEgress: "runtime",
		HedgeDelay:      0.5,
	}); err != nil {
		t.Fatalf("竞速应成功：%v", err)
	}
	if len(order) < 1 {
		t.Fatal("应至少发出一次请求")
	}
}

func scriptedRedirectClient(responses []http.Response) func(Channel) (*http.Client, error) {
	return func(Channel) (*http.Client, error) {
		var idx atomic.Int32
		return &http.Client{
			Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
				i := int(idx.Add(1)) - 1
				if i >= len(responses) {
					i = len(responses) - 1
				}
				r := responses[i]
				hdr := http.Header{}
				for k, v := range r.Header {
					hdr[k] = v
				}
				body := []byte(nil)
				if r.Body != nil {
					body, _ = io.ReadAll(r.Body)
					r.Body = io.NopCloser(bytes.NewReader(body))
				}
				return &http.Response{
					StatusCode: r.StatusCode,
					Header:     hdr,
					Body:       io.NopCloser(bytes.NewReader(body)),
					Request:    req,
				}, nil
			}),
		}, nil
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestFetchRedirectPolicyAndSSRFAndSizeLimit(t *testing.T) {
	ch := []Channel{{Name: ChannelDirect, EgressID: ChannelDirect}}

	res, err := FetchSubscription(context.Background(), "http://a.invalid/sub", ch, FetchOptions{
		ClientFactory: scriptedRedirectClient([]http.Response{
			{StatusCode: 302, Header: http.Header{"Location": {"http://b.invalid:8443/s2"}}},
			{StatusCode: 302, Header: http.Header{"Location": {"https://c.invalid/s3"}}},
			{StatusCode: 200, Body: io.NopCloser(strings.NewReader(validSubscriptionBody))},
		}),
	})
	if err != nil || res.Fmt != FormatYAML {
		t.Fatalf("<=3 hops should succeed: res=%v err=%v", res, err)
	}

	_, err = FetchSubscription(context.Background(), "http://a.invalid/sub", ch, FetchOptions{
		ClientFactory: scriptedRedirectClient([]http.Response{
			{StatusCode: 302, Header: http.Header{"Location": {"http://b.invalid/1"}}},
			{StatusCode: 302, Header: http.Header{"Location": {"http://c.invalid/2"}}},
			{StatusCode: 302, Header: http.Header{"Location": {"http://d.invalid/3"}}},
			{StatusCode: 302, Header: http.Header{"Location": {"http://e.invalid/4"}}},
			{StatusCode: 200, Body: io.NopCloser(strings.NewReader(validSubscriptionBody))},
		}),
	})
	if err == nil || !strings.Contains(err.Error(), "订阅地址重定向过多") || !strings.Contains(err.Error(), "provider_redirect_blocked") {
		t.Fatalf("4th hop should fail with redirect limit: %v", err)
	}

	called := false
	_, err = FetchSubscription(context.Background(), "file:///etc/passwd", ch, FetchOptions{
		ClientFactory: func(Channel) (*http.Client, error) {
			called = true
			return nil, errors.New("should not call")
		},
	})
	if err == nil || called || !strings.Contains(err.Error(), "http/https") {
		t.Fatalf("bad initial scheme failed check: called=%v err=%v", called, err)
	}

	for _, loc := range []string{"file:///C:/Windows/win.ini", "http://127.0.0.1:8080/internal", "http://169.254.169.254/latest/meta-data/"} {
		_, err = FetchSubscription(context.Background(), "http://a.invalid/sub", ch, FetchOptions{
			ClientFactory: scriptedRedirectClient([]http.Response{
				{StatusCode: 302, Header: http.Header{"Location": {loc}}},
				{StatusCode: 200, Body: io.NopCloser(strings.NewReader(validSubscriptionBody))},
			}),
		})
		if err == nil || !strings.Contains(err.Error(), "provider_redirect_blocked") || strings.Contains(err.Error(), loc) {
			t.Fatalf("unsafe redirect %q not blocked properly: %v", loc, err)
		}
	}

	pad9M := validSubscriptionBody + strings.Repeat("# comment padding\n", 500000)
	if len(pad9M) <= 8<<20 {
		t.Fatalf("expected > 8MiB fixture, got %d", len(pad9M))
	}
	res, err = FetchSubscription(context.Background(), "http://a.invalid/9m", ch, FetchOptions{
		ClientFactory: scriptedRedirectClient([]http.Response{
			{StatusCode: 200, Body: io.NopCloser(strings.NewReader(pad9M))},
		}),
	})
	if err != nil || len(res.ResolvedNodes) != 1 {
		t.Fatalf("9MiB body should succeed under 20MiB limit: err=%v", err)
	}
	oversized := strings.Repeat("x", (20<<20)+1)
	_, err = FetchSubscription(context.Background(), "http://a.invalid/huge", ch, FetchOptions{
		ClientFactory: scriptedRedirectClient([]http.Response{
			{StatusCode: 200, Body: io.NopCloser(strings.NewReader(oversized))},
		}),
	})
	if err == nil || !strings.Contains(err.Error(), "provider_size_limit") || !strings.Contains(err.Error(), "订阅内容过大") {
		t.Fatalf(">20MiB should fail with size limit: %v", err)
	}
}

func TestFetchCancelExitsPromptlyWithoutSpin(t *testing.T) {
	started := make(chan struct{})
	unblock := make(chan struct{})
	t.Cleanup(func() {
		select {
		case <-unblock:
		default:
			close(unblock)
		}
	})
	var calls atomic.Int32
	factory := func(Channel) (*http.Client, error) {
		return &http.Client{
			Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
				if calls.Add(1) == 1 {
					close(started)
				}
				<-req.Context().Done()
				select {
				case <-time.After(300 * time.Millisecond):
				case <-unblock:
				}
				return nil, req.Context().Err()
			}),
		}, nil
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		_, err := FetchSubscription(ctx, "http://sub.example.com/s", []Channel{
			{Name: "a", EgressID: "a"},
			{Name: "b", EgressID: "b"},
		}, FetchOptions{
			ClientFactory: factory,
			TotalTimeout:  10,
			HedgeDelay:    0.5,
		})
		done <- err
	}()

	<-started
	canceledAt := time.Now()
	cancel()

	select {
	case err := <-done:
		if err == nil {
			t.Fatal("取消后 FetchSubscription 应返回错误")
		}
		if elapsed := time.Since(canceledAt); elapsed > 120*time.Millisecond {
			t.Fatalf("取消后未立即退出循环，耗时=%v", elapsed)
		}
		if got := calls.Load(); got != 1 {
			t.Fatalf("取消后不应继续发射对冲通道，实际调用=%d", got)
		}
	case <-time.After(1 * time.Second):
		t.Fatal("取消后 FetchSubscription 卡死或空转")
	}
}

func TestDefaultClientFactoryBlocksDirectSSRFAndDNSFailure(t *testing.T) {
	var hits atomic.Int32
	localSrv := muxServer(t, func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(validSubscriptionBody))
	})

	direct := []Channel{{Name: ChannelDirect, EgressID: ChannelDirect}}
	for _, target := range []string{
		localSrv.URL + "/sub",
		"http://127.0.0.1:9/sub",
		"http://[::1]:9/sub",
		"http://169.254.169.254/latest/meta-data/",
		"http://100.100.100.200/latest/meta-data/",
		"http://198.18.0.1/sub",
		"http://localhost:8080/sub",
	} {
		_, err := FetchSubscription(context.Background(), target, direct, FetchOptions{
			RequestTimeout: 1,
			TotalTimeout:   2,
		})
		if err == nil {
			t.Fatalf("expected SSRF rejection for %s", target)
		}
		var ffe *FetchFailedError
		if !errors.As(err, &ffe) || len(ffe.Attempts) == 0 || ffe.Attempts[0].Stage != "policy" {
			t.Fatalf("target %s expected policy rejection attempt, got err=%v", target, err)
		}
	}
	if hits.Load() != 0 {
		t.Fatalf("local httptest server received %d direct SSRF hits, want 0", hits.Load())
	}

	u, _ := url.Parse("http://unresolvable-domain-for-ssrf-check.invalid/sub")
	if err := checkRedirectTarget(context.Background(), u, false); err == nil {
		t.Fatal("expected checkRedirectTarget to fail closed on DNS lookup error")
	}
}
