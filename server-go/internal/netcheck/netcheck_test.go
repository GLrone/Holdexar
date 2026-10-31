package netcheck

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// 测试全程用本地 httptest 桩或关死端口作探测目标，不触真实网络。

type eventLog struct {
	mu     sync.Mutex
	events []string
}

func (l *eventLog) record(level, format string, args ...any) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.events = append(l.events, level+": "+format)
}

func (l *eventLog) all() []string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return append([]string(nil), l.events...)
}

// fastChecker 测试参数：阈值 2、单轮探测、零间隔，事件入 log。
func fastChecker(log *eventLog) *Checker {
	return &Checker{
		Threshold:    2,
		PingAttempts: 1,
		PingRetryGap: time.Millisecond,
		PollInterval: time.Millisecond,
		Logf:         log.record,
	}
}

func deadURL(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("占位监听失败：%v", err)
	}
	url := "http://" + ln.Addr().String()
	_ = ln.Close() // 关死端口：探测必然连接拒绝
	return url
}

func TestSuccessResetsConsecutiveFailures(t *testing.T) {
	c := fastChecker(&eventLog{})
	c.PingURLs = []string{deadURL(t)} // 探测必败：计数只能靠成功请求清零

	c.ReportFailure()
	c.ReportSuccess() // 怀疑计数清零
	c.ReportFailure()
	if c.IsOffline() {
		t.Fatalf("成功清零后再失败一次不应到线确认断网")
	}
	c.ReportFailure()
	if !c.IsOffline() {
		t.Fatalf("连续两次传输失败且探测全败应确认断网")
	}
	c.ReportSuccess()
	if c.IsOffline() {
		t.Fatalf("成功收发应直接推翻断网判定")
	}
}

func TestLiveProbeVetoesOffline(t *testing.T) {
	log := &eventLog{}
	c := fastChecker(log)
	// 探测目标返回 503：状态码无关紧要，拿到响应即证明链路通
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer srv.Close()
	c.PingURLs = []string{deadURL(t), srv.URL} // 首目标死、次目标活

	for range 3 {
		c.ReportFailure()
	}
	if c.IsOffline() {
		t.Fatalf("直连探测有响应应否决断网判定")
	}
	if len(log.all()) == 0 || !strings.Contains(strings.Join(log.all(), "\n"), "判定为代理或目标侧问题") {
		t.Fatalf("应记录「代理或目标侧问题」判定，实际事件：%v", log.all())
	}
}

func TestConfirmOfflineWhenProbeFails(t *testing.T) {
	c := fastChecker(&eventLog{})
	c.PingURLs = []string{deadURL(t), deadURL(t)}

	for range 2 {
		c.ReportFailure()
	}
	if !c.IsOffline() {
		t.Fatalf("到线且探测全败应确认断网")
	}
	// 断网期间不再累计：继续上报不改变状态，也不触发新核实
	for range 5 {
		c.ReportFailure()
	}
	if !c.IsOffline() {
		t.Fatalf("断网态应保持")
	}
}

func TestWaitUntilOnlineRecovers(t *testing.T) {
	c := fastChecker(&eventLog{})
	dead := deadURL(t)
	c.PingURLs = []string{dead}
	for range 2 {
		c.ReportFailure()
	}
	if !c.IsOffline() {
		t.Fatalf("前置：应先确认断网")
	}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {}))
	defer srv.Close()
	c.PingURLs = []string{dead, srv.URL} // 恢复后探测可通
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if !c.WaitUntilOnline(ctx) {
		t.Fatalf("探测恢复后应返回 true")
	}
	if c.IsOffline() {
		t.Fatalf("恢复后状态应清零")
	}
}

func TestWaitUntilOnlineStopsOnContextCancel(t *testing.T) {
	c := fastChecker(&eventLog{})
	c.PingURLs = []string{deadURL(t)}
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(20 * time.Millisecond)
		cancel()
	}()
	if c.WaitUntilOnline(ctx) {
		t.Fatalf("停止信号应返回 false")
	}
}

func TestThresholdNotReachedNoProbe(t *testing.T) {
	log := &eventLog{}
	c := fastChecker(log)
	c.PingURLs = []string{deadURL(t)}
	c.ReportFailure()
	if c.IsOffline() || len(log.all()) != 0 {
		t.Fatalf("未到阈值不应核实也不应产生判定事件，实际：%v", log.all())
	}
}
