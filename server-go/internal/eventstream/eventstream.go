// Package eventstream：SSE 流式端点——事件总线流（/api/v1/events/stream）与
// 运行日志流（/api/v1/system/logs/stream）。响应头与帧节奏对齐 Python 侧
// StreamingResponse：15s 空闲注释帧防中间层断连，X-Accel-Buffering: no。
package eventstream

import (
	"context"
	"errors"
	"net/http"
	"sync"
	"time"

	"holdexar/server-go/internal/events"
)

// defaultKeepAlive 无事件时的注释帧间隔；前端 EventSource 与中间层都以
// 有字节流动判定连接存活。
const defaultKeepAlive = 15 * time.Second

// logReplay 日志流接入时回放的缓冲行数：进页面即见上下文。
const logReplay = 200

// Streamer 两条 SSE 流端点的依赖。KeepAlive 零值取 15s；测试注入短间隔。
type Streamer struct {
	Bus  *events.Bus
	Ring *Ring

	KeepAlive time.Duration
}

// Register 装配 SSE 端点。路径与前端 EventSource 直连地址一致。
func Register(mux *http.ServeMux, bus *events.Bus, ring *Ring) {
	s := &Streamer{Bus: bus, Ring: ring}
	mux.HandleFunc("GET /api/v1/events/stream", s.handleEvents)
	mux.HandleFunc("GET /api/v1/system/logs/stream", s.handleLogs)
}

func (s *Streamer) keepAlive() time.Duration {
	if s.KeepAlive > 0 {
		return s.KeepAlive
	}
	return defaultKeepAlive
}

// writeHeaders 流式响应头：text/event-stream 带 charset（对齐 Starlette 对
// text/* 的缺省补全），代理层禁缓冲。
func writeHeaders(w http.ResponseWriter) {
	h := w.Header()
	h.Set("Content-Type", "text/event-stream; charset=utf-8")
	h.Set("Cache-Control", "no-cache")
	h.Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
}

// writeFrame 写一帧并立即冲刷；连接已断时返回 false，调用方收尾。
func writeFrame(w http.ResponseWriter, frame string) bool {
	if _, err := w.Write([]byte(frame)); err != nil {
		return false
	}
	if f, ok := w.(http.Flusher); ok {
		f.Flush()
	}
	return true
}

// handleEvents 事件总线流：连上先发 connected 注释帧，事件以 event/data 帧
// 下发，空闲 15s 发 keep-alive 注释帧；断连即注销订阅。
func (s *Streamer) handleEvents(w http.ResponseWriter, r *http.Request) {
	sub := s.Bus.Subscribe()
	defer s.Bus.Unsubscribe(sub)

	writeHeaders(w)
	if !writeFrame(w, ": connected\n\n") {
		return
	}
	for {
		ctx, cancel := context.WithTimeout(r.Context(), s.keepAlive())
		ev, err := sub.Get(ctx)
		cancel()
		if err != nil {
			if errors.Is(err, context.DeadlineExceeded) {
				if !writeFrame(w, ": keep-alive\n\n") {
					return
				}
				continue
			}
			return // 客户端断连（context canceled）：defer 完成注销
		}
		if !writeFrame(w, ev.AsSSE()) {
			return
		}
	}
}

// handleLogs 运行日志流：先回放缓冲内最近 200 行再续播增量（对齐 Python
// 先订阅后回放的次序，回放与订阅窗口之间到达的行会重复一次，现网同行为）。
func (s *Streamer) handleLogs(w http.ResponseWriter, r *http.Request) {
	ch := s.Ring.Subscribe()
	defer s.Ring.Unsubscribe(ch)

	writeHeaders(w)
	for _, line := range s.Ring.Snapshot(logReplay) {
		if !writeFrame(w, "data: "+events.PyJSONString(line)+"\n\n") {
			return
		}
	}
	if !writeFrame(w, ": synced\n\n") {
		return
	}
	for {
		ctx, cancel := context.WithTimeout(r.Context(), s.keepAlive())
		select {
		case line := <-ch:
			cancel()
			if !writeFrame(w, "data: "+events.PyJSONString(line)+"\n\n") {
				return
			}
		case <-ctx.Done():
			if errors.Is(ctx.Err(), context.DeadlineExceeded) {
				cancel()
				if !writeFrame(w, ": keep-alive\n\n") {
					return
				}
				continue
			}
			cancel()
			return // 客户端断连
		}
	}
}

// Ring 运行日志环形缓冲：保留最近 capacity 行，供回放与订阅。写入方任意
// goroutine 安全；订阅者队列满挤旧保新——日志只保证尽量实时，不背压。
type Ring struct {
	mu     sync.Mutex
	buf    []string // 定长环形；head 为最旧行下标，size 为当前行数
	head   int
	subs   map[chan string]struct{}
	subCap int
}

// NewRing 创建环形缓冲；订阅者队列容量 1000（与 Python 同款）。
func NewRing(capacity int) *Ring {
	if capacity < 1 {
		capacity = 1
	}
	return &Ring{
		buf:    make([]string, 0, capacity),
		subs:   make(map[chan string]struct{}),
		subCap: 1000,
	}
}

// Write 追加一行并广播订阅者。锁内广播（非阻塞投递）：保证广播次序与
// 缓冲次序一致。
func (r *Ring) Write(line string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.buf) < cap(r.buf) {
		r.buf = append(r.buf, line)
	} else {
		r.buf[r.head] = line
		r.head = (r.head + 1) % len(r.buf)
	}
	for ch := range r.subs {
		select {
		case ch <- line:
		default:
			select { // 满：挤掉最旧保留最新
			case <-ch:
			default:
			}
			select {
			case ch <- line:
			default:
			}
		}
	}
}

// Snapshot 最近 limit 行（旧→新）。
func (r *Ring) Snapshot(limit int) []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	n := len(r.buf)
	if limit < n {
		n = limit
	}
	out := make([]string, n)
	for i := 0; i < n; i++ {
		out[i] = r.buf[(r.head+len(r.buf)-n+i)%len(r.buf)]
	}
	return out
}

// Subscribe 登记新订阅者队列。
func (r *Ring) Subscribe() chan string {
	ch := make(chan string, r.subCap)
	r.mu.Lock()
	r.subs[ch] = struct{}{}
	r.mu.Unlock()
	return ch
}

// Unsubscribe 注销订阅者。
func (r *Ring) Unsubscribe(ch chan string) {
	r.mu.Lock()
	delete(r.subs, ch)
	r.mu.Unlock()
}

// Len 当前订阅数（观测与测试用）。
func (r *Ring) Len() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.subs)
}
