package plugins

import (
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"net/netip"
	"strings"
	"time"
)

var blockedNetworks = []netip.Prefix{
	netip.MustParsePrefix("0.0.0.0/8"), netip.MustParsePrefix("100.64.0.0/10"), netip.MustParsePrefix("192.0.0.0/24"), netip.MustParsePrefix("192.0.2.0/24"), netip.MustParsePrefix("198.18.0.0/15"), netip.MustParsePrefix("198.51.100.0/24"), netip.MustParsePrefix("203.0.113.0/24"), netip.MustParsePrefix("240.0.0.0/4"), netip.MustParsePrefix("2001:db8::/32"), netip.MustParsePrefix("64:ff9b::/96"), netip.MustParsePrefix("64:ff9b:1::/48"), netip.MustParsePrefix("2002::/16"), netip.MustParsePrefix("2001::/32"),
}

func publicAddress(ip netip.Addr) bool {
	ip = ip.Unmap()
	if !ip.IsGlobalUnicast() || ip.IsPrivate() || ip.IsLoopback() || ip.IsLinkLocalUnicast() || ip.IsMulticast() || ip.Zone() != "" {
		return false
	}
	for _, prefix := range blockedNetworks {
		if prefix.Contains(ip) {
			return false
		}
	}
	return true
}

func ValidateHTTPS(raw string, domains []string) error {
	u, e := parsedURL(raw)
	if e != nil {
		return e
	}
	host := strings.ToLower(u.Hostname())
	if strings.HasSuffix(host, ".") || host == "localhost" || strings.HasSuffix(host, ".localhost") || strings.HasSuffix(host, ".local") || strings.HasSuffix(host, ".internal") {
		return errors.New("private plugin addresses are not allowed")
	}
	if ip, e := netip.ParseAddr(host); e == nil && !publicAddress(ip) {
		return errors.New("private plugin addresses are not allowed")
	}
	if len(domains) > 0 {
		found := false
		for _, domain := range domains {
			if host == domain {
				found = true
			}
		}
		if !found {
			return errors.New("plugin endpoint is outside its declared domains")
		}
	}
	return nil
}

type safeTransport struct{ base http.RoundTripper }

func (s safeTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	if e := ValidateHTTPS(r.URL.String(), nil); e != nil {
		return nil, e
	}
	return s.base.RoundTrip(r)
}

func NewSafeClient() *http.Client {
	transport := &http.Transport{
		Proxy: nil, TLSHandshakeTimeout: 8 * time.Second, ResponseHeaderTimeout: 10 * time.Second, IdleConnTimeout: 30 * time.Second, MaxIdleConns: 20,
		DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
			host, port, e := net.SplitHostPort(address)
			if e != nil {
				return nil, errors.New("invalid plugin network address")
			}
			ips, e := net.DefaultResolver.LookupNetIP(ctx, "ip", host)
			if e != nil || len(ips) == 0 {
				return nil, errors.New("plugin host lookup failed")
			}
			for _, ip := range ips {
				if !publicAddress(ip) {
					return nil, errors.New("plugin host resolves to a private address")
				}
			}
			dialer := &net.Dialer{Timeout: 8 * time.Second}
			var conn net.Conn
			for _, ip := range ips {
				conn, e = dialer.DialContext(ctx, network, net.JoinHostPort(ip.String(), port))
				if e == nil {
					return conn, nil
				}
			}
			return nil, errors.New("plugin connection failed")
		},
	}
	return &http.Client{Transport: safeTransport{base: transport}, Timeout: 20 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return errors.New("plugin redirects are not allowed") }}
}

func (m *Manager) download(ctx context.Context, raw string, limit int64) ([]byte, error) {
	if e := ValidateHTTPS(raw, nil); e != nil {
		return nil, e
	}
	req, e := http.NewRequestWithContext(ctx, "GET", raw, nil)
	if e != nil {
		return nil, errors.New("invalid plugin download")
	}
	response, e := m.client.Do(req)
	if e != nil {
		return nil, errors.New("plugin source is unavailable")
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, errors.New("plugin source returned an unsuccessful response")
	}
	if response.ContentLength > limit {
		return nil, errors.New("plugin download is too large")
	}
	b, e := io.ReadAll(io.LimitReader(response.Body, limit+1))
	if e != nil {
		return nil, errors.New("plugin download was interrupted")
	}
	if int64(len(b)) > limit {
		return nil, errors.New("plugin download is too large")
	}
	return b, nil
}
