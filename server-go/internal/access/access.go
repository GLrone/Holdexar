// Package access：本地接口准入——进程内会话令牌 + Host/Origin 校验。
//
// 信任边界与现网一致的三类对手：本机其他进程（挡在「拿不到进程内令牌」）、
// 恶意网页（DNS 重绑定由 Host 校验挡、跨端口 Cookie 携带由 Origin 校验挡）、
// 桌面壳（同进程直取令牌，不走网络）。
package access

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/url"
	"strings"
	"sync"
)

// 会话令牌的两种承载：HttpOnly Cookie（SSE 唯一能带凭据的载体）与请求头。
const (
	SessionCookie = "holdexar_session"
	SessionHeader = "X-Holdexar-Session"
	apiPrefix     = "/api/"
)

// PublicPaths 无会话即可访问的 API 路径。健康检查是判活唯一入口；更新验收
// 确认要在页面拿到会话之前调用。清单是唯一事实源，禁止在路由里另行标注。
var PublicPaths = map[string]bool{
	"/api/v1/health":              true,
	"/api/v1/system/update-ready": true,
	"/api/v1/session/bootstrap":   true,
	"/api/v1/session/dev":         true,
}

// Gate 持有进程内会话令牌与 MCP 闸挂点。MCP 面未接线时 MCP 前缀为空。
type Gate struct {
	mu    sync.Mutex
	token string

	mcpPrefix string
	mcpGate   func(*http.Request) int // 0=放行，401/404=拒绝；由 MCP 面接线时注册
}

// New 创建访问闸；令牌首次取用时生成。
func New() *Gate { return &Gate{} }

// Token 当前进程的会话令牌（首次调用生成，进程退出即失效）。
// 不落盘、不进日志、不进任何响应体。
func (g *Gate) Token() string {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.token == "" {
		buf := make([]byte, 32)
		if _, err := rand.Read(buf); err != nil {
			panic("会话令牌生成失败：" + err.Error())
		}
		g.token = base64.RawURLEncoding.EncodeToString(buf)
	}
	return g.token
}

// Reset 丢弃当前令牌（测试隔离用）。
func (g *Gate) Reset() {
	g.mu.Lock()
	g.token = ""
	g.mu.Unlock()
}

// RegisterMCPGate 注册 MCP 路由闸：Host/Origin 之后、会话之前生效。
// 返回 0 放行；401 凭据缺失；404 未启用。MCP 面接线前不注册即一律 404。
func (g *Gate) RegisterMCPGate(pathPrefix string, gate func(*http.Request) int) {
	g.mu.Lock()
	g.mcpPrefix, g.mcpGate = pathPrefix, gate
	g.mu.Unlock()
}

func (g *Gate) mcpVerdict(path string, r *http.Request) (int, bool) {
	g.mu.Lock()
	prefix, gate := g.mcpPrefix, g.mcpGate
	g.mu.Unlock()
	if prefix == "" || !strings.HasPrefix(path, prefix) {
		return 0, false
	}
	if gate == nil {
		return http.StatusNotFound, true
	}
	return gate(r), true
}

func cookieValue(r *http.Request) string {
	for _, part := range strings.Split(r.Header.Get("Cookie"), ";") {
		key, raw, _ := strings.Cut(strings.TrimSpace(part), "=")
		if key == SessionCookie {
			return raw
		}
	}
	return ""
}

func (g *Gate) authorized(r *http.Request) bool {
	expected := g.Token()
	for _, candidate := range []string{cookieValue(r), r.Header.Get(SessionHeader)} {
		if candidate != "" && subtle.ConstantTimeCompare([]byte(candidate), []byte(expected)) == 1 {
			return true
		}
	}
	return false
}

// HostAllowed 只接受回环 Host，挡住 DNS 重绑定：重绑定后浏览器带来的 Host
// 是攻击者域名，端口不限（端口协商会换），只看回环名。
func HostAllowed(host string) bool {
	host = strings.ToLower(strings.TrimSpace(host))
	if host == "" {
		return false
	}
	// IPv6 字面量形如 [::1]:port
	if strings.HasPrefix(host, "[") {
		return strings.HasPrefix(host, "[::1]")
	}
	if i := strings.LastIndex(host, ":"); i >= 0 {
		host = host[:i]
	}
	return host == "127.0.0.1" || host == "localhost"
}

// OriginAllowed 请求来源是否属于本应用自身。Cookie 作用域不含端口，跨端口
// 页面会自动带上本应用会话 Cookie，必须由服务端校验 Origin。规则：带 Origin
// 时必须与请求 Host 同源（scheme 不校验、端口必须相等）；不带放行；
// Origin: null 属外部来源一律拒绝。
func OriginAllowed(r *http.Request) bool {
	origin := strings.TrimSpace(r.Header.Get("Origin"))
	if origin == "" {
		return true
	}
	if origin == "null" {
		return false
	}
	parts, err := url.Parse(origin)
	if err != nil || (parts.Scheme != "http" && parts.Scheme != "https") {
		return false
	}
	if !HostAllowed(parts.Host) {
		return false
	}
	request, err := url.Parse("//" + r.Host)
	if err != nil {
		return false
	}
	originPort := parts.Port()
	if originPort == "" {
		if parts.Scheme == "https" {
			originPort = "443"
		} else {
			originPort = "80"
		}
	}
	requestPort := request.Port()
	if requestPort == "" {
		if r.TLS != nil {
			requestPort = "443"
		} else {
			requestPort = "80"
		}
	}
	return parts.Hostname() == request.Hostname() && originPort == requestPort
}

// Middleware 统一准入：所有 /api/ 路径默认要求会话，公开路径显式白名单。
// 非 API 路径（SPA 静态与页面回退）不含用户数据，不拦。
func (g *Gate) Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path := r.URL.Path
		if !strings.HasPrefix(path, apiPrefix) {
			next.ServeHTTP(w, r)
			return
		}
		if !HostAllowed(r.Host) {
			writeJSON(w, http.StatusMisdirectedRequest, map[string]any{"detail": "请求来源不被信任"})
			return
		}
		if r.Method == http.MethodOptions {
			next.ServeHTTP(w, r)
			return
		}
		if !OriginAllowed(r) {
			writeJSON(w, http.StatusForbidden, map[string]any{"detail": "请求来源与应用不同源"})
			return
		}
		if verdict, hit := g.mcpVerdict(path, r); hit {
			if verdict == 0 {
				next.ServeHTTP(w, r)
				return
			}
			detail := "缺少有效的 MCP 令牌"
			if verdict == http.StatusNotFound {
				detail = "not found"
			}
			writeJSON(w, verdict, map[string]any{"detail": detail})
			return
		}
		if PublicPaths[path] {
			next.ServeHTTP(w, r)
			return
		}
		if !g.authorized(r) {
			writeJSON(w, http.StatusUnauthorized, map[string]any{"detail": "缺少有效的本地会话"})
			return
		}
		next.ServeHTTP(w, r)
	})
}

// Issue 把会话令牌写进响应 Cookie（HttpOnly + SameSite=Strict；回环 HTTP 下
// 不能置 Secure，浏览器会拒收）。
func (g *Gate) Issue(w http.ResponseWriter) {
	http.SetCookie(w, &http.Cookie{
		Name:     SessionCookie,
		Value:    g.Token(),
		Path:     "/",
		HttpOnly: true,
		SameSite: http.SameSiteStrictMode,
	})
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	body, err := json.Marshal(payload)
	if err != nil {
		http.Error(w, "响应序列化失败", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(body)
}
