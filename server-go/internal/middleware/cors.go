// Package middleware：HTTP 中间件——CORS 与安全响应头，线行为对齐 Python
// 侧 Starlette CORSMiddleware 与 main.py 的 _add_static_cache_headers。
// 叠序（请求流向）：安全响应头 → 访问闸 → CORS → 路由。
package middleware

import (
	"net/http"
	"strconv"
	"strings"
)

// allMethods Starlette "*" 展开的完整方法表（含 QUERY，序同源）。
var allMethods = []string{"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "QUERY"}

// safelistedHeaders 显式 AllowHeaders 清单下仍恒放行的浏览器安全表头。
var safelistedHeaders = []string{"accept", "accept-language", "content-language", "content-type"}

// CORS 跨域中间件（Starlette CORSMiddleware 同径）。预检（OPTIONS + Origin
// 头 + Access-Control-Request-Method 头）在此直接应答不进内层；简单响应按
// Origin 是否在清单补放行头与 Vary。
type CORS struct {
	AllowOrigins     []string
	AllowCredentials bool
	AllowMethods     []string // 含 "*" 展开为全方法表
	AllowHeaders     []string // 含 "*" 时镜像请求的 Access-Control-Request-Headers
	MaxAge           int      // 秒；零值取 600
}

func (c CORS) Middleware(next http.Handler) http.Handler {
	methods := c.AllowMethods
	if len(methods) == 1 && methods[0] == "*" {
		methods = allMethods
	}
	allowAllHeaders := len(c.AllowHeaders) == 1 && c.AllowHeaders[0] == "*"
	allowAllOrigins := len(c.AllowOrigins) == 1 && c.AllowOrigins[0] == "*"
	maxAge := c.MaxAge
	if maxAge <= 0 {
		maxAge = 600
	}
	allowHeaders := make([]string, 0, len(c.AllowHeaders)+len(safelistedHeaders))
	for _, name := range c.AllowHeaders {
		allowHeaders = append(allowHeaders, strings.ToLower(name))
	}
	allowHeaders = append(allowHeaders, safelistedHeaders...)

	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		origin, hasOrigin := r.Header["Origin"]
		_, hasACRM := r.Header["Access-Control-Request-Method"]
		if hasOrigin && r.Method == http.MethodOptions && hasACRM {
			c.preflight(w, r, origin[0], methods, allowAllHeaders, allowAllOrigins, maxAge, allowHeaders)
			return
		}
		cw := &corsWriter{ResponseWriter: w, cors: c, allowAllOrigins: allowAllOrigins}
		if hasOrigin {
			cw.origin, cw.hasOrigin = origin[0], true
		}
		next.ServeHTTP(cw, r)
	})
}

// preflight 预检应答（Starlette preflight_response 同径）：失败集合非空回
// 400「Disallowed CORS <原因>」，全过回 200「OK」；两者都携带预检基础头。
func (c CORS) preflight(w http.ResponseWriter, r *http.Request, origin string, methods []string,
	allowAllHeaders, allowAllOrigins bool, maxAge int, allowHeaders []string) {
	h := w.Header()
	h.Set("Content-Type", "text/plain; charset=utf-8")
	h.Set("Vary", "Origin, Access-Control-Request-Method, Access-Control-Request-Headers, Access-Control-Request-Private-Network")
	h.Set("Access-Control-Allow-Methods", strings.Join(methods, ", "))
	h.Set("Access-Control-Max-Age", strconv.Itoa(maxAge))
	if c.AllowCredentials {
		h.Set("Access-Control-Allow-Credentials", "true")
	}
	var failures []string
	if c.originAllowed(origin, allowAllOrigins) {
		// 清单放行时回具体 Origin；全放行且不带凭据才回 "*"
		if !allowAllOrigins || c.AllowCredentials {
			h.Set("Access-Control-Allow-Origin", origin)
		} else {
			h.Set("Access-Control-Allow-Origin", "*")
		}
	} else {
		failures = append(failures, "origin")
	}
	if !contains(methods, r.Header.Get("Access-Control-Request-Method")) {
		failures = append(failures, "method")
	}
	if requestedHeaders := r.Header.Get("Access-Control-Request-Headers"); requestedHeaders != "" {
		if allowAllHeaders {
			h.Set("Access-Control-Allow-Headers", requestedHeaders)
		} else {
			for _, name := range strings.Split(requestedHeaders, ",") {
				if !contains(allowHeaders, strings.ToLower(strings.TrimSpace(name))) {
					failures = append(failures, "headers")
					break
				}
			}
		}
	}
	if r.Header.Get("Access-Control-Request-Private-Network") != "" {
		failures = append(failures, "private-network")
	}
	if len(failures) > 0 {
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte("Disallowed CORS " + strings.Join(failures, ", ")))
		return
	}
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("OK"))
}

// originAllowed Origin 是否在放行清单（全放行为 true）。
func (c CORS) originAllowed(origin string, allowAllOrigins bool) bool {
	if allowAllOrigins {
		return true
	}
	return contains(c.AllowOrigins, origin)
}

// corsWriter 简单响应头补写在 WriteHeader 前完成（Starlette 在响应起始帧
// 注入同径）；未显式写头的路径由 Write / Flush 兜底。
type corsWriter struct {
	http.ResponseWriter
	cors            CORS
	allowAllOrigins bool
	origin          string // 请求侧 Origin（响应头里不会出现）
	hasOrigin       bool
	wroteHeader     bool
}

func (w *corsWriter) WriteHeader(code int) {
	if w.wroteHeader {
		return
	}
	w.wroteHeader = true
	h := w.Header()
	switch {
	case w.hasOrigin && w.allowAllOrigins && w.cors.AllowCredentials:
		// Starlette simple_headers + send() 的放行矩阵：凭据位在 Origin
		// 在场时恒下发；具体 Origin 与 "*" 互斥，未命中只补 Vary。
		if w.cors.AllowCredentials {
			h.Set("Access-Control-Allow-Credentials", "true")
		}
		h.Set("Access-Control-Allow-Origin", w.origin)
		appendVary(h, "Origin")
	case w.hasOrigin && !w.allowAllOrigins && w.cors.originAllowed(w.origin, false):
		h.Set("Access-Control-Allow-Credentials", "true")
		h.Set("Access-Control-Allow-Origin", w.origin)
		appendVary(h, "Origin")
	case w.hasOrigin && w.allowAllOrigins:
		h.Set("Access-Control-Allow-Origin", "*")
		appendVary(h, "Origin")
	default:
		// 未命中清单（含无 Origin 头）：只补 Vary（Starlette else 分支同径）。
		if w.hasOrigin && w.cors.AllowCredentials {
			h.Set("Access-Control-Allow-Credentials", "true")
		}
		appendVary(h, "Origin")
	}
	w.ResponseWriter.WriteHeader(code)
}

func (w *corsWriter) Write(b []byte) (int, error) {
	if !w.wroteHeader {
		w.WriteHeader(http.StatusOK)
	}
	return w.ResponseWriter.Write(b)
}

func (w *corsWriter) Flush() {
	if !w.wroteHeader {
		w.WriteHeader(http.StatusOK)
	}
	if f, ok := w.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

// appendVary 追加一个 Vary 值（Python getlist 合并同径）。
func appendVary(h http.Header, val string) {
	if v := h.Get("Vary"); v != "" {
		h.Set("Vary", v+", "+val)
		return
	}
	h.Set("Vary", val)
}

func contains(list []string, v string) bool {
	for _, s := range list {
		if s == v {
			return true
		}
	}
	return false
}
