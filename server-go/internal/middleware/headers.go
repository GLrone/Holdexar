package middleware

import (
	"encoding/json"
	"net/http"
	"regexp"
	"strings"
)

// cspValue 内容安全策略（script-src 仅 'self'，img-src 含 Steam/Epic/GP/HB 媒体域）。
const cspValue = "default-src 'self'; img-src 'self' data: https://cdn.cloudflare.steamstatic.com " +
	"https://shared.akamai.steamstatic.com https://store.fastly.steamstatic.com " +
	"https://avatars.fastly.steamstatic.com " +
	"https://*.epicgames.com https://*.epicgamescdn.com https://*.unrealengine.com " +
	"https://*.gamerpower.com https://*.file.myqcloud.com https://hb-prd.imgix.net; " +
	"style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; " +
	"frame-ancestors 'none'; object-src 'none'; base-uri 'self'"

const updateBusyDetail = "正在完成更新，请稍后再试"

var fingerprintedName = regexp.MustCompile(`-[A-Za-z0-9_-]{8}\.\w+$`)

var staticSuffixes = map[string]bool{
	".js": true, ".css": true, ".png": true, ".jpg": true, ".jpeg": true,
	".webp": true, ".gif": true, ".svg": true, ".ico": true, ".woff": true,
	".woff2": true, ".ttf": true, ".otf": true, ".map": true,
}

// Headers 进入内层前预置安全头与 Cache-Control（内层可 Set 覆盖），并在保护位开启时拦截非只读 /api/ 请求。
type Headers struct {
	// WriteProtected 更新验收期的写保护位；nil 视为未保护。
	WriteProtected func() bool
}

func (h Headers) Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hd := w.Header()
		if cache := cacheControlFor(r.URL.Path); cache != "" {
			hd.Set("Cache-Control", cache)
		}
		setSecurityHeaders(hd)
		if h.protected() && strings.HasPrefix(r.URL.Path, "/api/") &&
			r.Method != http.MethodGet && r.Method != http.MethodHead &&
			r.Method != http.MethodOptions && r.URL.Path != "/api/v1/system/update-ready" {
			hd.Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusServiceUnavailable)
			body, _ := json.Marshal(map[string]any{"detail": updateBusyDetail})
			_, _ = w.Write(body)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func setSecurityHeaders(h http.Header) {
	h.Set("X-Content-Type-Options", "nosniff")
	h.Set("X-Frame-Options", "DENY")
	h.Set("Referrer-Policy", "no-referrer")
	h.Set("Content-Security-Policy", cspValue)
}

func (h Headers) protected() bool {
	return h.WriteProtected != nil && h.WriteProtected()
}

// cacheControlFor API/文档返回空串；8 位指纹静态件一年 immutable，其余静态件一天，余者 no-cache。
func cacheControlFor(urlPath string) string {
	p := strings.TrimLeft(urlPath, "/")
	if strings.HasPrefix(p, "api/") || strings.HasPrefix(p, "docs") ||
		strings.HasPrefix(p, "redoc") || strings.HasPrefix(p, "openapi.json") {
		return ""
	}
	name := p
	if i := strings.LastIndexByte(p, '/'); i >= 0 {
		name = p[i+1:]
	}
	dot := strings.LastIndexByte(name, '.')
	if name != "" && dot >= 0 && staticSuffixes[strings.ToLower(name[dot:])] {
		if fingerprintedName.MatchString(name) {
			return "public, max-age=31536000, immutable"
		}
		return "public, max-age=86400"
	}
	return "no-cache"
}
