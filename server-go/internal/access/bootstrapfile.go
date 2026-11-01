// 引导票据文件协议：服务进程健康就绪后把一次性引导票据写进约定路径，
// 桌面壳轮询读取后导航引导端点换会话 Cookie。文件只承载 nonce——会话
// 令牌本体任何时刻不落盘；票据被消费或过期后由服务端删除。会话发放
// 复用既有引导端点，不新增第二条会话通道。
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

// 文件存活期须覆盖引导票据的有效期（60s），过期清理留少量宽限避免
// 与在途请求竞速。
const (
	bootstrapFileTTL   = 60 * time.Second
	bootstrapFileGrace = 5 * time.Second
)

// BootstrapFile 持有票据文件路径与清理计时。
type BootstrapFile struct {
	path string
	ttl  time.Duration
	// Log 清理失败等运营告警出口（nil 静默）；只写路径与原因，不写票据内容。
	Log func(format string, args ...any)

	mu    sync.Mutex
	timer *time.Timer
}

// NewBootstrapFile 创建票据文件协议实例；父目录由调用方（桌面壳）创建。
func NewBootstrapFile(path string) *BootstrapFile {
	return &BootstrapFile{path: path, ttl: bootstrapFileTTL + bootstrapFileGrace}
}

// Publish 写入票据（0600）并启动过期清理计时。
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

// Consumed 票据已成功换发会话：撤销过期计时并立即删除文件。
func (b *BootstrapFile) Consumed() {
	b.mu.Lock()
	b.stopTimerLocked()
	b.mu.Unlock()
	b.discard("票据已消费")
}

// Close 进程退出兜底：文件仍在场则删除。
func (b *BootstrapFile) Close() {
	b.discard("进程退出")
}

func (b *BootstrapFile) stopTimerLocked() {
	if b.timer != nil {
		b.timer.Stop()
		b.timer = nil
	}
}

// discard 删除票据文件：尽力而为；残留文件内容是已失效的 nonce，
// 下次启动会被新票据覆盖。
func (b *BootstrapFile) discard(reason string) {
	err := os.Remove(b.path)
	if err == nil || errors.Is(err, fs.ErrNotExist) {
		return
	}
	if b.Log != nil {
		b.Log("引导票据文件清理失败（%s）：%v", reason, err)
	}
}

// Middleware 挂在会话闸外侧：引导端点以 303 发放会话视为票据消费，
// 删除文件；401（无效/过期）不删，交给过期计时。
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

// statusWriter 只为捕获引导端点的响应状态码。
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
