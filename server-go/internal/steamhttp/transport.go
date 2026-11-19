package steamhttp

import (
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"time"
)

type transportKey struct {
	proxy   string
	verify  bool
	timeout time.Duration
}

// transportFor：按 (代理, TLS 校验档, 超时) 缓存 *http.Transport 实例；请求带 Connection: close 且无 Cookie jar，不跨请求复用连接或会话。
func (c *Client) transportFor(verify bool) (*http.Transport, error) {
	key := transportKey{proxy: c.cfg.ProxyURL, verify: verify, timeout: c.cfg.Timeout}
	c.mu.Lock()
	if tr, ok := c.transports[key]; ok {
		c.mu.Unlock()
		return tr, nil
	}
	c.mu.Unlock()

	tr := &http.Transport{
		DialContext:           (&net.Dialer{Timeout: c.cfg.Timeout, KeepAlive: 30 * time.Second}).DialContext,
		TLSHandshakeTimeout:   c.cfg.Timeout,
		ResponseHeaderTimeout: c.cfg.Timeout,
		TLSClientConfig:       &tls.Config{InsecureSkipVerify: !verify},
		IdleConnTimeout:       90 * time.Second,
		// 自建 Transport 默认 HTTP/1.1：Connection: close 逐发生效
	}
	if c.cfg.ProxyURL != "" {
		u, err := url.Parse(c.cfg.ProxyURL)
		if err != nil {
			return nil, fmt.Errorf("代理地址无效 %q: %w", c.cfg.ProxyURL, err)
		}
		tr.Proxy = http.ProxyURL(u)
	}
	c.mu.Lock()
	c.transports[key] = tr
	c.mu.Unlock()
	return tr, nil
}

// classifyTransportError：超时记 timeout 可重试；dial/DNS/x509 证书错记 connect_error 并计数换出口；其余记 connect_error 但可重试。
func classifyTransportError(err error) (outcome string, connectClass bool) {
	var ne net.Error
	if errors.As(err, &ne) && ne.Timeout() {
		return OutcomeTimeout, false
	}
	var opErr *net.OpError
	if errors.As(err, &opErr) && opErr.Op == "dial" {
		return OutcomeConnectErr, true
	}
	var dnsErr *net.DNSError
	if errors.As(err, &dnsErr) {
		return OutcomeConnectErr, true
	}
	var authErr x509.UnknownAuthorityError
	var hostErr x509.HostnameError
	var certErr x509.CertificateInvalidError
	if errors.As(err, &authErr) || errors.As(err, &hostErr) || errors.As(err, &certErr) {
		return OutcomeConnectErr, true
	}
	return OutcomeConnectErr, false
}
