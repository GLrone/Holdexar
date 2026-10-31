package eventstream

import (
	"bufio"
	"context"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"holdexar/server-go/internal/events"
)

// newStreamer 短 keep-alive 的测试装配：50ms 节奏，避免真实 15s 等待。
func newStreamer(t *testing.T) (*Streamer, *events.Bus) {
	t.Helper()
	bus := events.NewBus()
	s := &Streamer{Bus: bus, Ring: NewRing(2000), KeepAlive: 50 * time.Millisecond}
	return s, bus
}

// readFrames 从响应流读取 n 帧（以空行分隔的帧块）。
func readFrames(t *testing.T, body *bufio.Reader, n int) []string {
	t.Helper()
	frames := make([]string, 0, n)
	sb := strings.Builder{}
	for len(frames) < n {
		line, err := body.ReadString('\n')
		if err != nil {
			t.Fatalf("读流失败（已得 %d 帧）: %v", len(frames), err)
		}
		sb.WriteString(line)
		if line == "\n" {
			frames = append(frames, sb.String())
			sb.Reset()
		}
	}
	return frames
}

func TestEventsStreamHeadersAndFrames(t *testing.T) {
	s, bus := newStreamer(t)
	srv := httptest.NewServer(http.HandlerFunc(s.handleEvents))
	defer srv.Close()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, "GET", srv.URL, nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("连接失败: %v", err)
	}
	defer resp.Body.Close()

	if ct := resp.Header.Get("Content-Type"); ct != "text/event-stream; charset=utf-8" {
		t.Errorf("Content-Type = %q", ct)
	}
	if cc := resp.Header.Get("Cache-Control"); cc != "no-cache" {
		t.Errorf("Cache-Control = %q", cc)
	}
	if ab := resp.Header.Get("X-Accel-Buffering"); ab != "no" {
		t.Errorf("X-Accel-Buffering = %q", ab)
	}

	body := bufio.NewReader(resp.Body)
	frames := readFrames(t, body, 1)
	if frames[0] != ": connected\n\n" {
		t.Fatalf("首帧 = %q, want connected 注释帧", frames[0])
	}

	bus.Publish("job.started", events.KV{Key: "job_id", Val: 5}, events.KV{Key: "count", Val: 3})
	frames = readFrames(t, body, 1)
	want := "event: job.started\ndata: {\"ts\": "
	if !strings.HasPrefix(frames[0], want) || !strings.HasSuffix(frames[0], ", \"count\": 3}\n\n") {
		t.Fatalf("事件帧形状不符: %q", frames[0])
	}
}

func TestEventsStreamKeepAliveCadence(t *testing.T) {
	s, _ := newStreamer(t) // 50ms 节奏
	srv := httptest.NewServer(http.HandlerFunc(s.handleEvents))
	defer srv.Close()

	resp, err := http.Get(srv.URL)
	if err != nil {
		t.Fatalf("连接失败: %v", err)
	}
	defer resp.Body.Close()
	body := bufio.NewReader(resp.Body)

	readFrames(t, body, 1) // connected
	start := time.Now()
	frames := readFrames(t, body, 3) // 三条 keep-alive
	elapsed := time.Since(start)
	for i, f := range frames {
		if f != ": keep-alive\n\n" {
			t.Fatalf("第 %d 帧 = %q, want keep-alive", i+1, f)
		}
	}
	// 节奏下限：三条帧至少经历 2 个完整间隔（读帧耗时计入，只验下限不卡上限）
	if elapsed < 90*time.Millisecond {
		t.Fatalf("三条 keep-alive 仅耗时 %v，节奏快于注入间隔", elapsed)
	}
}

func TestEventsStreamDisconnectUnsubscribes(t *testing.T) {
	s, _ := newStreamer(t)
	srv := httptest.NewServer(http.HandlerFunc(s.handleEvents))
	defer srv.Close()

	ctx, cancel := context.WithCancel(context.Background())
	req, _ := http.NewRequestWithContext(ctx, "GET", srv.URL, nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("连接失败: %v", err)
	}
	go func() {
		buf := make([]byte, 1024)
		for {
			if _, err := resp.Body.Read(buf); err != nil {
				resp.Body.Close()
				return
			}
		}
	}()
	// 等 connected 帧被消费后再断连
	time.Sleep(50 * time.Millisecond)
	if s.Bus.Len() != 1 {
		t.Fatalf("连接期间订阅数 = %d, want 1", s.Bus.Len())
	}
	cancel()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if s.Bus.Len() == 0 {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("断连后订阅数未回落：=%d（泄漏）", s.Bus.Len())
}

func TestLogsStreamReplayAndIncremental(t *testing.T) {
	// 长间隔装配：回放与增量之间不插 keep-alive，避免节奏竞态
	bus := events.NewBus()
	s := &Streamer{Bus: bus, Ring: NewRing(2000), KeepAlive: 5 * time.Second}
	ring := s.Ring
	for i := 0; i < 205; i++ {
		ring.Write("行" + itoa(i))
	}
	srv := httptest.NewServer(http.HandlerFunc(s.handleLogs))
	defer srv.Close()

	resp, err := http.Get(srv.URL)
	if err != nil {
		t.Fatalf("连接失败: %v", err)
	}
	defer resp.Body.Close()
	body := bufio.NewReader(resp.Body)

	// 回放恰好 200 行（旧→新，第 5..204 行），随后 synced
	frames := readFrames(t, body, logReplay+1)
	if got := frames[0]; got != "data: "+events.PyJSONString("行5")+"\n\n" {
		t.Fatalf("回放首行形状不符: %q", got)
	}
	if frames[logReplay] != ": synced\n\n" {
		t.Fatalf("回放后帧 = %q, want synced", frames[logReplay])
	}
	// 增量：订阅后写入的行到达
	ring.Write("增量行")
	frames = readFrames(t, body, 1)
	if frames[0] != "data: "+events.PyJSONString("增量行")+"\n\n" {
		t.Fatalf("增量帧形状不符: %q", frames[0])
	}
}

func TestLogsStreamDisconnectUnsubscribes(t *testing.T) {
	s, _ := newStreamer(t)
	srv := httptest.NewServer(http.HandlerFunc(s.handleLogs))
	defer srv.Close()

	ctx, cancel := context.WithCancel(context.Background())
	req, _ := http.NewRequestWithContext(ctx, "GET", srv.URL, nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("连接失败: %v", err)
	}
	go func() {
		buf := make([]byte, 1024)
		for {
			if _, err := resp.Body.Read(buf); err != nil {
				resp.Body.Close()
				return
			}
		}
	}()
	time.Sleep(50 * time.Millisecond)
	if s.Ring.Len() != 1 {
		t.Fatalf("连接期间订阅数 = %d, want 1", s.Ring.Len())
	}
	cancel()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if s.Ring.Len() == 0 {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("断连后订阅数未回落：=%d（泄漏）", s.Ring.Len())
}

func TestRingBackpressureDropsOldest(t *testing.T) {
	ring := NewRing(2000)
	ch := ring.Subscribe()
	// 灌满订阅者队列（1000）再灌 10 条：队列里是最新的 1000 条
	for i := 0; i < 1010; i++ {
		ring.Write(itoa(i))
	}
	select {
	case line := <-ch:
		if line != "10" {
			t.Fatalf("订阅队列首行 = %s, want 10（最旧被挤掉）", line)
		}
	default:
		t.Fatal("订阅队列空")
	}
}

func TestRingSnapshotWraps(t *testing.T) {
	ring := NewRing(5)
	for i := 0; i < 8; i++ {
		ring.Write(itoa(i))
	}
	got := ring.Snapshot(3)
	want := []string{"5", "6", "7"}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("环形回绕快照 = %v, want %v", got, want)
		}
	}
}

func itoa(i int) string { return strconv.Itoa(i) }
