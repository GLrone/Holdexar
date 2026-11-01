package middleware

import (
	"encoding/json"
	"net/http"
	"regexp"
	"strings"
)

// cspValue 内容安全策略（逐字对齐 Python，含空格排布）。
const cspValue = "default-src 'self'; img-src 'self' data: https://cdn.cloudflare.steamstatic.com " +
	"https://shared.akamai.steamstatic.com https://store.fastly.steamstatic.com; " +
	"style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; " +
	"frame-ancestors 'none'; object-src 'none'; base-uri 'self'"

// updateBusyDetail 更新验收期写拒绝的用户文案（逐字对齐）。
const updateBusyDetail = "正在完成更新，请稍后再试"

// fingerprintedName vite 构建产物的 8 位内容指纹（文件名换名换内容）。
var fingerprintedName = regexp.MustCompile(`-[A-Za-z0-9_-]{8}\.\w+$`)

// staticSuffixes 静态资源扩展名（无指纹公共件与指纹产物共用清单）。
var staticSuffixes = map[string]bool{
	".js": true, ".css": true, ".png": true, ".jpg": true, ".jpeg": true,
	".webp": true, ".gif": true, ".svg": true, ".ico": true, ".woff": true,
	".woff2": true, ".ttf": true, ".otf": true, ".map": true,
}

// Headers 安全响应头 + 静态缓存分档 + 更新验收期写保护（对齐 Python
// _add_static_cache_headers；本层处最外侧，访问闸与 CORS 的响应同样被套头）。
// 头在进入内层前预置；内层仍可 Set 覆盖（与 Python setdefault 的「内层已设
// 则不覆盖」在现有处理器集上等效，内层不得依赖外层后置头）。
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

// setSecurityHeaders 四个基础安全响应头（值逐字对齐 Python）。
func setSecurityHeaders(h http.Header) {
	h.Set("X-Content-Type-Options", "nosniff")
	h.Set("X-Frame-Options", "DENY")
	h.Set("Referrer-Policy", "no-referrer")
	h.Set("Content-Security-Policy", cspValue)
}

func (h Headers) protected() bool {
	return h.WriteProtected != nil && h.WriteProtected()
}

// cacheControlFor 按路径分档：API/文档路径回空串不干预；带 8 位指纹的产物
// 一年 immutable；无指纹静态件一天；其余（页面与回退 HTML）no-cache。
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
