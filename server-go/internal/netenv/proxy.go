package netenv

import (
	"context"
	"net"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

type resolver struct {
	mu          sync.Mutex
	environment func() []string
	registry    func() (bool, string)
	dial        func(context.Context, string, string) (net.Conn, error)
	now         func() time.Time
	cached      bool
	cachedAt    time.Time
	cachedURL   string
}

var systemResolver = &resolver{environment: os.Environ, registry: readWindowsProxy, dial: (&net.Dialer{Timeout: time.Second}).DialContext, now: time.Now}

// ResolveSystemProxy returns a reachable configured HTTP proxy, or an empty URL for direct access.
func ResolveSystemProxy(ctx context.Context) (string, error) { return systemResolver.resolve(ctx) }

func (r *resolver) resolve(ctx context.Context) (string, error) {
	if err := ctx.Err(); err != nil {
		return "", err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return "", err
	}
	now := r.now()
	if r.cached && now.Sub(r.cachedAt) < 30*time.Second {
		return r.cachedURL, nil
	}
	proxies := environmentProxies(r.environment())
	if len(proxies) == 0 {
		enabled, server := r.registry()
		proxies = registryProxies(enabled, server)
	}
	resolved := ""
	for _, candidate := range proxyCandidates(proxies) {
		u, err := url.Parse(candidate)
		if err != nil || u.Hostname() == "" || u.Port() == "" {
			continue
		}
		port, err := strconv.Atoi(u.Port())
		if err != nil || port < 1 || port > 65535 {
			continue
		}
		conn, err := r.dial(ctx, "tcp", net.JoinHostPort(u.Hostname(), u.Port()))
		if err == nil {
			_ = conn.Close()
			resolved = candidate
			break
		}
		if err := ctx.Err(); err != nil {
			return "", err
		}
	}
	r.cached = true
	r.cachedAt = now
	r.cachedURL = resolved
	return resolved, nil
}

func environmentProxies(environment []string) map[string]string {
	proxies := map[string]string{}
	cgi := false
	for _, entry := range environment {
		name, value, ok := strings.Cut(entry, "=")
		if !ok {
			continue
		}
		if name == "REQUEST_METHOD" {
			cgi = true
		}
		if len(name) >= 6 && strings.HasSuffix(strings.ToLower(name), "_proxy") && value != "" {
			proxies[strings.ToLower(name[:len(name)-6])] = value
		}
	}
	if cgi {
		delete(proxies, "http")
	}
	for _, entry := range environment {
		name, value, ok := strings.Cut(entry, "=")
		if !ok {
			continue
		}
		if len(name) >= 6 && strings.HasSuffix(name, "_proxy") {
			key := strings.ToLower(name[:len(name)-6])
			if value == "" {
				delete(proxies, key)
			} else {
				proxies[key] = value
			}
		}
	}
	return proxies
}

func registryProxies(enabled bool, server string) map[string]string {
	proxies := map[string]string{}
	if !enabled {
		return proxies
	}
	if !strings.ContainsAny(server, "=;") {
		server = "http=" + server + ";https=" + server + ";ftp=" + server
	}
	for _, part := range strings.Split(server, ";") {
		protocol, address, ok := strings.Cut(part, "=")
		if !ok {
			break
		}
		if !strings.Contains(address, "://") {
			switch protocol {
			case "http", "https", "ftp":
				address = "http://" + address
			case "socks":
				address = "socks://" + address
			}
		}
		proxies[protocol] = address
	}
	if socks := proxies["socks"]; socks != "" {
		if strings.HasPrefix(socks, "socks://") {
			socks = "socks4://" + strings.TrimPrefix(socks, "socks://")
		}
		for _, protocol := range []string{"http", "https"} {
			if proxies[protocol] == "" {
				proxies[protocol] = socks
			}
		}
	}
	return proxies
}

func proxyCandidates(proxies map[string]string) []string {
	var out []string
	for _, key := range []string{"https", "http"} {
		raw := proxies[key]
		if raw == "" {
			continue
		}
		if !strings.Contains(raw, "://") {
			raw = "http://" + raw
		}
		if !strings.HasPrefix(raw, "http://") && !strings.HasPrefix(raw, "https://") {
			continue
		}
		if len(out) == 0 || out[0] != raw {
			out = append(out, raw)
		}
	}
	return out
}
