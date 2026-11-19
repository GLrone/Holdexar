package eventstream

import (
	"context"
	"errors"
	"net/http"
	"sync"
	"time"

	"holdexar/server-go/internal/events"
	"holdexar/server-go/internal/logredact"
)

const defaultKeepAlive = 15 * time.Second

const logReplay = 200

type Streamer struct {
	Bus  *events.Bus
	Ring *Ring

	KeepAlive time.Duration
}

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

func writeHeaders(w http.ResponseWriter) {
	h := w.Header()
	h.Set("Content-Type", "text/event-stream; charset=utf-8")
	h.Set("Cache-Control", "no-cache")
	h.Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
}

func writeFrame(w http.ResponseWriter, frame string) bool {
	if _, err := w.Write([]byte(frame)); err != nil {
		return false
	}
	if f, ok := w.(http.Flusher); ok {
		f.Flush()
	}
	return true
}

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
			return
		}
		if !writeFrame(w, ev.AsSSE()) {
			return
		}
	}
}

// handleLogs 先订阅再回放近期缓冲以防丢行（窗口期内并发写入行可能重复）。
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
			return
		}
	}
}

// Ring 线程安全定长日志环形缓冲，订阅队列满时丢弃最旧行。
type Ring struct {
	mu     sync.Mutex
	buf    []string // 定长环形；len(buf) 为当前行数，回绕后 head 为最旧行下标
	head   int
	subs   map[chan string]struct{}
	subCap int
}

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

// Write 先脱敏再入环，并在锁内非阻塞广播以保证缓冲与订阅次序一致。
func (r *Ring) Write(line string) {
	line = logredact.Redact(line)
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
			select {
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

func (r *Ring) Subscribe() chan string {
	ch := make(chan string, r.subCap)
	r.mu.Lock()
	r.subs[ch] = struct{}{}
	r.mu.Unlock()
	return ch
}

func (r *Ring) Unsubscribe(ch chan string) {
	r.mu.Lock()
	delete(r.subs, ch)
	r.mu.Unlock()
}

func (r *Ring) Len() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.subs)
}
