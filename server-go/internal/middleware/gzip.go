// GZip 响应压缩（对齐 Starlette GZipMiddleware 线行为：compresslevel=6、
// minimum_size=500 按累计体量判定、text/event-stream 前缀豁免整条透传、
// Identity（无 gzip）对 ≥500 响应追加 Vary、阈值下不加 Vary）。
package middleware

import (
	"compress/gzip"
	"net/http"
	"strings"
)

const gzipMinSize = 500

// GZip 压缩中间件。挂载序对齐现网：本地准入之内、CORS 之外（安全响应头
// 仍在最外层）。豁免：响应已带 Content-Encoding、Content-Type 以
// text/event-stream 起始（含 charset 变体）——首块即定，整条透传。
type GZip struct{}

func (g GZip) Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gz := &gzipResponseWriter{ResponseWriter: w, req: r}
		next.ServeHTTP(gz, r)
		gz.finish()
	})
}

type gzipResponseWriter struct {
	http.ResponseWriter
	req         *http.Request
	status      int
	statusSent  bool
	decided     bool // 压缩/透传终态已定（豁免在首块即定；体量态在 ≥500 或收尾时定）
	passthrough bool
	compress    bool
	buf         []byte
	gz          *gzip.Writer
	varyDone    bool
}

func (g *gzipResponseWriter) Header() http.Header { return g.ResponseWriter.Header() }

func (g *gzipResponseWriter) WriteHeader(code int) {
	if g.statusSent || g.status != 0 {
		return
	}
	// 真暂存：状态行延迟到首块判定（decide/finish 的 flushStatus），
	// 压缩态的 Content-Encoding 改写才有机会落在发送之前。
	g.status = code
}

// flushStatus 下发状态行（压缩态须在改写头之后）。
func (g *gzipResponseWriter) flushStatus() {
	if g.status == 0 {
		g.status = http.StatusOK
	}
	g.statusSent = true
	g.ResponseWriter.WriteHeader(g.status)
}

func (g *gzipResponseWriter) Write(p []byte) (int, error) {
	if !g.decided {
		if g.exempt() {
			// SSE / 已带 Content-Encoding：与体量无关，整条透传
			g.passthrough = true
			g.decided = true
			g.flushStatus()
			return g.ResponseWriter.Write(p)
		}
		g.buf = append(g.buf, p...)
		if len(g.buf) >= gzipMinSize {
			if g.clientGzip() {
				g.startCompress()
			} else {
				g.addVary()
				g.passthrough = true
				g.decided = true
				g.flushStatus()
				return g.ResponseWriter.Write(g.buf)
			}
			g.decided = true
			return len(p), nil
		}
		return len(p), nil
	}
	if g.passthrough {
		return g.ResponseWriter.Write(p)
	}
	if g.compress {
		return g.gz.Write(p)
	}
	return len(p), nil
}

// finish 处理链返回：未跨过阈值的缓冲体透传（不加 Vary）；压缩流收尾。
func (g *gzipResponseWriter) finish() {
	if g.compress {
		g.gz.Close()
		return
	}
	if !g.decided && !g.statusSent {
		if len(g.buf) > 0 {
			g.flushStatus()
			_, _ = g.ResponseWriter.Write(g.buf)
			return
		}
		g.flushStatus() // 空体响应（304/204/HEAD）：仅状态
	}
}

// exempt 首块即定的豁免：SSE 前缀 / 已带 Content-Encoding。
func (g *gzipResponseWriter) exempt() bool {
	h := g.Header()
	return h.Get("Content-Encoding") != "" ||
		strings.HasPrefix(h.Get("Content-Type"), "text/event-stream")
}

func (g *gzipResponseWriter) clientGzip() bool {
	return strings.Contains(g.req.Header.Get("Accept-Encoding"), "gzip")
}

// startCompress 跨过阈值：改写响应头并以缓冲体初始化压缩流。
func (g *gzipResponseWriter) startCompress() {
	h := g.Header()
	h.Set("Content-Encoding", "gzip")
	g.addVary()
	h.Del("Content-Length")
	g.flushStatus()
	g.gz = gzip.NewWriter(g.ResponseWriter)
	_, _ = g.gz.Write(g.buf)
	g.buf = nil
	g.compress = true
}

// Flush 转发底层刷新：SSE 透传路径逐帧推送依赖此链；压缩路径先刷 gzip 流。
func (g *gzipResponseWriter) Flush() {
	if g.compress {
		_ = g.gz.Flush()
	}
	if f, ok := g.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

// addVary 对齐 starlette datastructures：已有 Vary 用 ", " 追加，否则设置。
func (g *gzipResponseWriter) addVary() {
	if g.varyDone {
		return
	}
	g.varyDone = true
	h := g.Header()
	existing := h.Get("Vary")
	if existing == "" {
		h.Set("Vary", "Accept-Encoding")
	} else if !strings.Contains(existing, "Accept-Encoding") {
		h.Set("Vary", existing+", Accept-Encoding")
	}
}
