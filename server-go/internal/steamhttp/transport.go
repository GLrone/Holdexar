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

// transportKey：传输层缓存键。超时进入键是因为拨号/TLS/响应头等待上限随
// 超时档变化，不同档不共享连接池。
type transportKey struct {
	proxy   string
	verify  bool
	timeout time.Duration
}

// transportFor：按 (代理, TLS 校验档, 超时) 缓存传输层——同参同实例，连接池
// 跨请求复用。请求逐发带 Connection: close 且客户端不挂 Cookie jar，响应
// Cookie 不留存，复用的只是 TCP/TLS 连接本身，无会话串味面。
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
		// 校验档按调用分档：跳过校验仅用于本机自签证书加速器场景
		TLSClientConfig: &tls.Config{InsecureSkipVerify: !verify},
		IdleConnTimeout: 90 * time.Second,
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

// 传输层错误分类：等待超时记 timeout 且可重试；建连类（拨号/DNS/TLS 握手）
// 记 connect_error 并计数换出口；其余传输错误记 connect_error 但可重试。
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
