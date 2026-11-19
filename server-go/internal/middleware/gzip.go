package middleware

import (
	"compress/gzip"
	"net/http"
	"strings"
)

const gzipMinSize = 500

// GZip 响应压缩中间件（阈值 500B，level 6）；跳过已有 Content-Encoding 或 text/event-stream。
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
	decided     bool
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
	// 暂存状态码至首块决策或 finish，以便压缩前修改 Content-Encoding/Content-Length。
	g.status = code
}

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
		g.flushStatus()
	}
}

func (g *gzipResponseWriter) exempt() bool {
	h := g.Header()
	return h.Get("Content-Encoding") != "" ||
		strings.HasPrefix(h.Get("Content-Type"), "text/event-stream")
}

func (g *gzipResponseWriter) clientGzip() bool {
	return strings.Contains(g.req.Header.Get("Accept-Encoding"), "gzip")
}

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
