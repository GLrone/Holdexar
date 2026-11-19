package access

import (
	"errors"
	"fmt"
	"io/fs"
	"net/http"
	"os"
	"sync"
	"time"
)

// bootstrapEndpoint 与 PublicPaths 里的引导端点同一路径（两处字面量，改动需同步）。
const bootstrapEndpoint = "/api/v1/session/bootstrap"

// 存活期覆盖 60s 引导票据有效期并留 5s 竞速宽限。
const (
	bootstrapFileTTL   = 60 * time.Second
	bootstrapFileGrace = 5 * time.Second
)

type BootstrapFile struct {
	path string
	ttl  time.Duration
	// Log 清理失败告警出口（nil 静默），禁写票据内容。
	Log func(format string, args ...any)

	mu    sync.Mutex
	timer *time.Timer
}

func NewBootstrapFile(path string) *BootstrapFile {
	return &BootstrapFile{path: path, ttl: bootstrapFileTTL + bootstrapFileGrace}
}

func (b *BootstrapFile) Publish(nonce string) error {
	if err := os.WriteFile(b.path, []byte(nonce), 0o600); err != nil {
		return fmt.Errorf("引导票据写盘失败：%w", err)
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	b.stopTimerLocked()
	b.timer = time.AfterFunc(b.ttl, func() {
		b.mu.Lock()
		b.timer = nil
		b.mu.Unlock()
		b.discard("票据过期")
	})
	return nil
}

func (b *BootstrapFile) Consumed() {
	b.mu.Lock()
	b.stopTimerLocked()
	b.mu.Unlock()
	b.discard("票据已消费")
}

func (b *BootstrapFile) Close() {
	b.discard("进程退出")
}

func (b *BootstrapFile) stopTimerLocked() {
	if b.timer != nil {
		b.timer.Stop()
		b.timer = nil
	}
}

func (b *BootstrapFile) discard(reason string) {
	err := os.Remove(b.path)
	if err == nil || errors.Is(err, fs.ErrNotExist) {
		return
	}
	if b.Log != nil {
		b.Log("引导票据文件清理失败（%s）：%v", reason, err)
	}
}

// Middleware 仅在引导端点返回 303 时删票据文件；401 保留待过期清理。
func (b *BootstrapFile) Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != bootstrapEndpoint {
			next.ServeHTTP(w, r)
			return
		}
		rec := &statusWriter{ResponseWriter: w}
		next.ServeHTTP(rec, r)
		if rec.status == http.StatusSeeOther {
			b.Consumed()
		}
	})
}

type statusWriter struct {
	http.ResponseWriter
	status int
	wrote  bool
}

func (w *statusWriter) WriteHeader(code int) {
	if !w.wrote {
		w.status, w.wrote = code, true
	}
	w.ResponseWriter.WriteHeader(code)
}

func (w *statusWriter) Write(p []byte) (int, error) {
	if !w.wrote {
		w.wrote = true
	}
	return w.ResponseWriter.Write(p)
}
