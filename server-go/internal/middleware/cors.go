// Package middleware 提供 CORS、GZip 压缩、安全响应头与静态缓存中间件。
package middleware

import (
	"net/http"
	"strconv"
	"strings"
)

var allMethods = []string{"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "QUERY"}

var safelistedHeaders = []string{"accept", "accept-language", "content-language", "content-type"}

// CORS 跨域中间件：拦截带 Origin 与 ACRM 的 OPTIONS 预检，简单响应补放行头与 Vary。
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

func (c CORS) preflight(w http.ResponseWriter, r *http.Request, origin string, methods []string,
	allowAllHeaders, allowAllOrigins bool, maxAge int, allowHeaders []string) {
	h := w.Header()
	h.Set("Content-Type", "text/plain; charset=utf-8")
	h.Set("Vary", "Origin, Access-Control-Request-Method, Access-Control-Request-Headers, Access-Control-Request-Private-Network")
	h.Set("Access-Control-Allow-Methods", strings.Join(methods, ", "))
	h.Set("Access-Control-Max-Age", strconv.Itoa(maxAge))
	var failures []string
	if c.originAllowed(origin, allowAllOrigins) {
		if c.AllowCredentials {
			h.Set("Access-Control-Allow-Credentials", "true")
		}
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

func (c CORS) originAllowed(origin string, allowAllOrigins bool) bool {
	if allowAllOrigins {
		return true
	}
	return contains(c.AllowOrigins, origin)
}

// corsWriter 在首个 WriteHeader / Write / Flush 前注入简单响应 CORS 头。
type corsWriter struct {
	http.ResponseWriter
	cors            CORS
	allowAllOrigins bool
	origin          string
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
	case w.hasOrigin && w.origin != "" && w.allowAllOrigins && w.cors.AllowCredentials:
		// 仅在 Origin 命中放行条件时下发凭据头与具体 Origin。
		h.Set("Access-Control-Allow-Credentials", "true")
		h.Set("Access-Control-Allow-Origin", w.origin)
		appendVary(h, "Origin")
	case w.hasOrigin && !w.allowAllOrigins && w.cors.originAllowed(w.origin, false):
		if w.cors.AllowCredentials {
			h.Set("Access-Control-Allow-Credentials", "true")
		}
		h.Set("Access-Control-Allow-Origin", w.origin)
		appendVary(h, "Origin")
	case w.hasOrigin && w.origin != "" && w.allowAllOrigins:
		h.Set("Access-Control-Allow-Origin", "*")
		appendVary(h, "Origin")
	default:
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
