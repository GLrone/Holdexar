package proxypool

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

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

// TestFetchHedgeWinner 双出口竞速：慢直连 + 快代理，快通道先返回有效内容获胜。
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

// TestFetchInvalidFastDoesNotWin 快通道返回无效内容：不获胜，慢通道的有效内容胜出。
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
	// 尝试留痕：快通道 unsupported + 慢通道成功，顺序按完成时间
	if len(res.Attempts) != 2 || res.Attempts[0].OK || !res.Attempts[1].OK {
		t.Fatalf("尝试留痕不符：%+v", res.Attempts)
	}
}

// TestFetchParallelismCap 单订阅出口竞速并发上限（默认 2）。
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

// TestFetchDeadline 所有出口挂起：整体 deadline 截停，留痕含 subscription_deadline。
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

// TestFetchValidationChain 内容校验链：非 200 / 空体 / 非 YAML / 编码坏。
func TestFetchValidationChain(t *testing.T) {
	cases := []struct {
		name   string
		status int
		body   string
		want   string // 期望错误类型
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
	// UA 必须带 clash 关键字（面板按 UA 分流订阅格式）
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

// TestEgressPreference 首选出口排序：preferred 优先启动。
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
	// 两个通道同一服务器，仅 egress id 不同；preferred 应先发车
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
