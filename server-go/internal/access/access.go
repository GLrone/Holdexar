// Package access 本地 HTTP 准入（会话令牌、Host 重绑定校验与 Origin 校验）。
package access

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"strings"
	"sync"
	"time"

	"holdexar/server-go/internal/httpx"
)

// 会话令牌的两种承载：HttpOnly Cookie（SSE 唯一能带凭据的载体）与请求头。
const (
	SessionCookie = "holdexar_session"
	SessionHeader = "X-Holdexar-Session"
	apiPrefix     = "/api/"
)

// PublicPaths 无需会话即可访问的 API 路径白名单（唯一事实源）。
var PublicPaths = map[string]bool{
	"/api/v1/health":                        true,
	"/api/v1/system/update-ready":           true,
	"/api/v1/session/bootstrap":             true,
	"/api/v1/session/dev":                   true,
	"/api/v1/session/unlock":                true,
	"/api/v1/session/recovery/login/start":  true,
	"/api/v1/session/recovery/login/code":   true,
	"/api/v1/session/recovery/login/status": true,
	"/api/v1/session/recovery/complete":     true,
	"/api/v1/session/recovery/cancel":       true,
}

type Gate struct {
	mu       sync.Mutex
	token    string
	sessions map[string]time.Time
	now      func() time.Time

	mcpPrefix string
	mcpGate   func(*http.Request) int // 0=放行，401/404=拒绝；由 MCP 面接线时注册
}

func New() *Gate { return &Gate{sessions: map[string]time.Time{}} }

func (g *Gate) nowTime() time.Time {
	if g.now != nil {
		return g.now()
	}
	return time.Now()
}

// sessionTTL 会话有效期（12h），过期条目在后续闸判定或签发时清理。
const sessionTTL = 12 * time.Hour

// Token 返回进程内兼容会话令牌（仅测试使用），受 sessionTTL 约束并按需补发。
func (g *Gate) Token() string {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.pruneLocked()
	if g.token == "" {
		g.token = g.newSessionLocked()
	}
	return g.token
}

func (g *Gate) newSessionLocked() string {
	g.pruneLocked()
	if g.sessions == nil {
		g.sessions = map[string]time.Time{}
	}
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		panic("会话令牌生成失败：" + err.Error())
	}
	tok := base64.RawURLEncoding.EncodeToString(buf)
	g.sessions[tok] = g.nowTime().Add(sessionTTL)
	return tok
}

func (g *Gate) pruneLocked() {
	now := g.nowTime()
	for tok, exp := range g.sessions {
		if !exp.After(now) {
			delete(g.sessions, tok)
		}
	}
	if g.token != "" {
		if _, ok := g.sessions[g.token]; !ok {
			g.token = ""
		}
	}
}

// Reset 清空全部已签发会话并置空兼容令牌（安全锁定时由 InvalidateSessions 调用）。
func (g *Gate) Reset() {
	g.mu.Lock()
	clear(g.sessions)
	g.token = ""
	g.mu.Unlock()
}

func (g *Gate) InvalidateSessions() { g.Reset() }

// RegisterMCPGate 注册 MCP 前缀闸（Host/Origin 之后、会话之前执行）；gate 为 nil 时该前缀一律 404。
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
	for _, header := range r.Header.Values("Cookie") {
		for _, part := range strings.Split(header, ";") {
			key, raw, _ := strings.Cut(strings.TrimSpace(part), "=")
			if key == SessionCookie {
				return raw
			}
		}
	}
	return ""
}

func (g *Gate) authorized(r *http.Request) bool {
	if r == nil {
		return false
	}
	c1 := cookieValue(r)
	c2 := r.Header.Get(SessionHeader)
	if c1 == "" && c2 == "" {
		return false
	}
	g.mu.Lock()
	g.pruneLocked()
	matched := false
	for tok := range g.sessions {
		if c1 != "" && subtle.ConstantTimeCompare([]byte(c1), []byte(tok)) == 1 {
			matched = true
		}
		if c2 != "" && subtle.ConstantTimeCompare([]byte(c2), []byte(tok)) == 1 {
			matched = true
		}
	}
	if !matched && g.token != "" {
		if c1 != "" && subtle.ConstantTimeCompare([]byte(c1), []byte(g.token)) == 1 {
			matched = true
		}
		if c2 != "" && subtle.ConstantTimeCompare([]byte(c2), []byte(g.token)) == 1 {
			matched = true
		}
	}
	g.mu.Unlock()
	return matched
}

func (g *Gate) Authorized(r *http.Request) bool {
	return g.authorized(r)
}

// PeerIsLoopback 对端物理地址是否回环（含 IPv4-mapped 回环）；空地址与非回环地址均返回 false。
func PeerIsLoopback(remoteAddr string) bool {
	remoteAddr = strings.TrimSpace(remoteAddr)
	if remoteAddr == "" {
		return false
	}
	host, _, err := net.SplitHostPort(remoteAddr)
	if err != nil || host == "" {
		host = remoteAddr
	}
	host = strings.TrimSuffix(strings.TrimPrefix(host, "["), "]")
	addr, err := netip.ParseAddr(host)
	if err != nil {
		return false
	}
	return addr.Unmap().IsLoopback()
}

// 局域网模式放行的本机地址集合（字面量、小写、无端口）；空＝默认回环-only。
var (
	extraHostsMu sync.RWMutex
	extraHosts   map[string]struct{}
)

func SetExtraHosts(hosts []string) {
	normalized := map[string]struct{}{}
	for _, host := range hosts {
		if hostname := hostnameOf(host); hostname != "" {
			normalized[hostname] = struct{}{}
		}
	}
	extraHostsMu.Lock()
	extraHosts = normalized
	extraHostsMu.Unlock()
}

func extraHostsHas(host string) bool {
	hostname := hostnameOf(host)
	if hostname == "" {
		return false
	}
	extraHostsMu.RLock()
	defer extraHostsMu.RUnlock()
	_, ok := extraHosts[hostname]
	return ok
}

// HostAllowed 只放行回环与已登记本机字面地址，防御 DNS 重绑定。
func HostAllowed(host string) bool {
	return LoopbackHost(host) || extraHostsHas(host)
}

// LoopbackHost 仅用于 Host 校验防御 DNS 重绑定；不可作来源可信判定（可伪造）。
func LoopbackHost(host string) bool {
	switch hostnameOf(host) {
	case "127.0.0.1", "localhost", "::1":
		return true
	}
	return false
}

// hostnameOf 提取小写主机名；端口非纯数字时不剥离（防 127.0.0.1:evil 绕过）。
func hostnameOf(host string) string {
	host = strings.ToLower(strings.TrimSpace(host))
	if host == "" {
		return ""
	}
	if strings.HasPrefix(host, "[") {
		if end := strings.Index(host, "]"); end > 0 {
			return host[1:end]
		}
		return ""
	}
	if strings.Count(host, ":") > 1 {
		return host
	}
	if i := strings.LastIndex(host, ":"); i >= 0 {
		port := host[i+1:]
		if port != "" && isDigits(port) {
			return host[:i]
		}
		return host
	}
	return host
}

func isDigits(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return false
		}
	}
	return true
}

// OriginAllowed 带 Origin 时须与 Host 同源且同端口（Cookie 作用域不含端口）；Origin: null 一律拒绝。
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

// Middleware 校验 Host/Origin、MCP 闸与会话；未鉴权非 API 请求只返回独立解锁页。
func (g *Gate) Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path := r.URL.Path
		if !HostAllowed(r.Host) {
			writeJSON(w, http.StatusMisdirectedRequest, map[string]any{"detail": "请求来源不被信任"})
			return
		}
		// 拦截非回环对端伪造回环 Host。
		if LoopbackHost(r.Host) && !PeerIsLoopback(r.RemoteAddr) {
			writeJSON(w, http.StatusMisdirectedRequest, map[string]any{"detail": "请求来源不被信任"})
			return
		}
		if !LANEnabled() && !extraHostsHas(r.Host) && !PeerIsLoopback(r.RemoteAddr) {
			writeJSON(w, http.StatusMisdirectedRequest, map[string]any{"detail": "请求来源不被信任"})
			return
		}
		if !strings.HasPrefix(path, apiPrefix) {
			if !g.authorized(r) {
				serveLockedPage(w, r)
				return
			}
			next.ServeHTTP(w, r)
			return
		}
		// OPTIONS 仅放行回环对端或已鉴权请求，阻断未鉴权局域网对端探测方法。
		if r.Method == http.MethodOptions {
			if PeerIsLoopback(r.RemoteAddr) || g.authorized(r) {
				next.ServeHTTP(w, r)
				return
			}
			writeJSON(w, http.StatusForbidden, map[string]any{"detail": "请求来源不被信任"})
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

// Issue 签发 per-login 会话令牌并写入 HttpOnly + SameSite=Strict Cookie（回环 HTTP 不置 Secure）。
func (g *Gate) Issue(w http.ResponseWriter) {
	g.mu.Lock()
	tok := g.newSessionLocked()
	g.mu.Unlock()
	http.SetCookie(w, &http.Cookie{
		Name:     SessionCookie,
		Value:    tok,
		Path:     "/",
		HttpOnly: true,
		SameSite: http.SameSiteStrictMode,
	})
}

func (g *Gate) RevokeSession(w http.ResponseWriter, r *http.Request) {
	if r != nil {
		c1 := cookieValue(r)
		c2 := r.Header.Get(SessionHeader)
		g.mu.Lock()
		for _, candidate := range []string{c1, c2} {
			if candidate == "" {
				continue
			}
			delete(g.sessions, candidate)
			if g.token != "" && subtle.ConstantTimeCompare([]byte(candidate), []byte(g.token)) == 1 {
				g.token = ""
			}
		}
		g.mu.Unlock()
	}
	if w != nil {
		http.SetCookie(w, &http.Cookie{
			Name:     SessionCookie,
			Value:    "",
			Path:     "/",
			Expires:  time.Unix(0, 0).UTC(),
			MaxAge:   -1,
			HttpOnly: true,
			SameSite: http.SameSiteStrictMode,
		})
	}
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	httpx.WriteJSON(w, status, payload)
}
