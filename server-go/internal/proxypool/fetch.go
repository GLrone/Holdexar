package proxypool

// 网络请求全部发生在写闸之外；任何失败都保留最后一次成功快照。

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"

	"holdexar/server-go/internal/clashcfg"
)

// SUB_USER_AGENT 必须含 clash 关键字且版本对齐内核，防机场回退或 404。
const SUB_USER_AGENT = "clash.meta/1.19.32"

const (
	DefaultFetchTimeout         = 12.0
	DefaultSubscriptionDeadline = 24.0
	DefaultHedgeDelay           = 0.25
	DefaultFetchParallelism     = 2
)

const (
	FormatYAML         = "yaml"
	FormatProviderYAML = "provider_yaml"
	FormatBase64       = "base64"
	FormatURI          = "uri"
	FormatAge          = "age"
	FormatHTML         = "html"
	FormatJSON         = "json"
	FormatEmpty        = "empty"
	FormatUnknown      = "unknown"
)

const (
	ChannelDirect = "direct"
	ChannelKernel = "mihomo"
	ChannelPool   = "proxypool"
	ChannelLocal  = "local"
)

type Channel struct {
	Name     string
	Proxy    string
	EgressID string
}

type SnapshotError struct{ msg string }

func (e *SnapshotError) Error() string { return e.msg }

type FetchFailedError struct {
	URL        string
	Attempts   []FetchAttempt
	DurationMS int
}

func (e *FetchFailedError) Error() string {
	shown := make([]string, 0, len(e.Attempts))
	for _, a := range e.Attempts {
		detail := a.Error
		if a.HTTPStatus != nil {
			detail = fmt.Sprintf("%d", *a.HTTPStatus)
		}
		shown = append(shown, fmt.Sprintf("%s:%s", a.Channel, detail))
	}
	return fmt.Sprintf("订阅抓取失败（%d 通道）：%s", len(e.Attempts), strings.Join(shown, "、"))
}

type UnsupportedFormatError struct {
	Format     string
	Attempts   []FetchAttempt
	DurationMS int
}

func (e *UnsupportedFormatError) Error() string {
	switch e.Format {
	case "provider_proxy_group_unsupported":
		return "订阅节点来源引用了同一配置中的代理组作为下载出口；该出口在节点注册前不可用"
	case "provider_proxy_unavailable":
		return "订阅节点来源指定的代理出口当前不可用"
	case "provider_direct_route_unavailable":
		return "订阅节点来源要求直连，但当前没有直连通道"
	case "provider_proxy_rejected":
		return "订阅节点来源的代理设置拒绝连接"
	case "provider_redirect_blocked":
		return "订阅节点来源重定向过多"
	case "provider_size_limit":
		return "订阅内容过大"
	}
	return fmt.Sprintf("不支持的订阅格式：%s", e.Format)
}

type RedirectPolicyError struct {
	Reason   string
	Attempts []FetchAttempt
}

func (e *RedirectPolicyError) Error() string {
	switch e.Reason {
	case "bad_scheme":
		return "订阅地址不支持（仅 http/https）"
	case "too_many_redirects":
		return "订阅地址重定向过多"
	case "private_ip":
		return "订阅地址重定向目标受限"
	default:
		return "订阅地址策略受限"
	}
}

type SubscriptionTooLargeError struct {
	Attempts []FetchAttempt
}

func (e *SubscriptionTooLargeError) Error() string { return "订阅内容过大" }

type EmptyNodeSetError struct {
	Format     string
	Attempts   []FetchAttempt
	DurationMS int
}

func (e *EmptyNodeSetError) Error() string {
	return fmt.Sprintf("订阅解析出 0 个节点（%s），按失败处理", e.Format)
}

type InvalidEncodingError struct {
	Attempts   []FetchAttempt
	DurationMS int
	Detail     string
}

func (e *InvalidEncodingError) Error() string {
	if e.Detail != "" {
		return "订阅内容不是合法 UTF-8：" + e.Detail
	}
	return "订阅内容不是合法 UTF-8"
}

// ParseNodes 仅支持严格 UTF-8 + YAML，其余格式一律拒绝。
func ParseNodes(raw []byte, fmtStr string) ([]map[string]any, error) {
	if fmtStr != FormatYAML {
		return nil, &UnsupportedFormatError{Format: fmtStr}
	}
	text, err := decodeUTF8Strict(raw)
	if err != nil {
		return nil, &InvalidEncodingError{}
	}
	doc, err := clashcfg.LoadYAML(text)
	if err != nil {
		return nil, err
	}
	if _, hasProviders := doc["proxy-providers"].(map[string]any); hasProviders {
		px, isList := doc["proxies"].([]any)
		if !isList || len(px) == 0 {
			return nil, &UnsupportedFormatError{Format: FormatProviderYAML}
		}
	}
	px, _ := doc["proxies"].([]any)
	var nodes []map[string]any
	for _, item := range px {
		m, ok := item.(map[string]any)
		if !ok {
			continue
		}
		if !pyTruthy(m["name"]) {
			continue
		}
		if IsInfoPlaceholder(pyStr(m["name"])) {
			continue
		}
		nodes = append(nodes, m)
	}
	if len(nodes) == 0 {
		return nil, &EmptyNodeSetError{Format: fmtStr}
	}
	return nodes, nil
}

var (
	reProvidersKey = regexp.MustCompile(`(?m)^proxy-providers[\t\n\v\f\r \x1c-\x1f\x85\xa0\p{Zs}]*:`)
	reProxiesKey   = regexp.MustCompile(`(?m)^proxies[\t\n\v\f\r \x1c-\x1f\x85\xa0\p{Zs}]*:`)
	reURIScheme    = regexp.MustCompile(`^[a-z0-9+.\-]+://`)
	reBase64Body   = regexp.MustCompile(`^[A-Za-z0-9+/=]+$`)
)

func DetectFormat(raw []byte) (string, error) {
	if len(raw) == 0 {
		return FormatEmpty, nil
	}
	text, err := decodeUTF8Strict(raw)
	if err != nil {
		return "", &InvalidEncodingError{}
	}
	if strings.Contains(truncateRunes(text, 2000), "-----BEGIN AGE ENCRYPTED FILE-----") {
		return FormatAge, nil
	}
	head := truncateRunes(pyLStrip(text), 400)
	if strings.HasPrefix(head, "<") {
		return FormatHTML, nil
	}
	if strings.HasPrefix(head, "{") || strings.HasPrefix(head, "[") {
		return FormatJSON, nil
	}
	if reProvidersKey.MatchString(text) {
		return FormatProviderYAML, nil
	}
	if reProxiesKey.MatchString(text) {
		return FormatYAML, nil
	}
	var lines []string
	for _, ln := range pySplitLines(text) {
		if t := pyTrimSpace(ln); t != "" {
			lines = append(lines, t)
			if len(lines) == 5 {
				break
			}
		}
	}
	allURI := len(lines) > 0
	for _, ln := range lines {
		if !reURIScheme.MatchString(ln) {
			allURI = false
			break
		}
	}
	if allURI {
		return FormatURI, nil
	}
	var body strings.Builder
	for _, r := range text {
		if !isPySpace(r) {
			body.WriteRune(r)
		}
	}
	if body.Len() > 40 && reBase64Body.MatchString(body.String()) {
		return FormatBase64, nil
	}
	return FormatUnknown, nil
}

func decodeUTF8Strict(raw []byte) (string, error) {
	if !utf8.Valid(raw) {
		return "", &InvalidEncodingError{}
	}
	return string(raw), nil
}

func pySplitLines(s string) []string {
	return strings.FieldsFunc(s, func(r rune) bool {
		switch r {
		case '\n', '\r', '\v', '\f', 0x1c, 0x1d, 0x1e, 0x85, 0x2028, 0x2029:
			return true
		}
		return false
	})
}

func pyLStrip(s string) string {
	return strings.TrimLeftFunc(s, pythonStripRunes)
}

func isPySpace(r rune) bool {
	switch r {
	case '\t', '\n', '\v', '\f', '\r', 0x1c, 0x1d, 0x1e, 0x1f, 0x85, 0xa0,
		0x1680, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000:
		return true
	}
	return r >= 0x2000 && r <= 0x200a
}

type ClientFactory func(channel Channel) (*http.Client, error)

// defaultDirectDialer 仅供单测注入本地 httptest 直连拨号，生产运行恒为 nil。
var defaultDirectDialer func(ctx context.Context, network, address string) (net.Conn, error)

// SetDirectDialerForTest 供跨包集成测试临时注入直连拨号器并返回复位函数。
func SetDirectDialerForTest(dialer func(ctx context.Context, network, address string) (net.Conn, error)) func() {
	prev := defaultDirectDialer
	defaultDirectDialer = dialer
	return func() { defaultDirectDialer = prev }
}

func safeDirectDialContext(ctx context.Context, network, address string) (net.Conn, error) {
	host, port, err := net.SplitHostPort(address)
	if err != nil {
		return nil, &RedirectPolicyError{Reason: "private_ip"}
	}
	lower := strings.ToLower(strings.TrimSpace(host))
	if lower == "" || strings.HasSuffix(lower, ".") || lower == "localhost" ||
		strings.HasSuffix(lower, ".localhost") || strings.HasSuffix(lower, ".local") ||
		strings.HasSuffix(lower, ".internal") {
		return nil, &RedirectPolicyError{Reason: "private_ip"}
	}
	if ip, parseErr := netip.ParseAddr(lower); parseErr == nil {
		if !isPublicRedirectIP(ip) {
			return nil, &RedirectPolicyError{Reason: "private_ip"}
		}
		dialer := &net.Dialer{Timeout: 8 * time.Second}
		return dialer.DialContext(ctx, network, net.JoinHostPort(ip.Unmap().String(), port))
	}
	ips, err := net.DefaultResolver.LookupNetIP(ctx, "ip", host)
	if err != nil || len(ips) == 0 {
		return nil, errors.New("订阅主机解析失败")
	}
	for _, ip := range ips {
		if !isPublicRedirectIP(ip) {
			return nil, &RedirectPolicyError{Reason: "private_ip"}
		}
	}
	dialer := &net.Dialer{Timeout: 8 * time.Second}
	var lastErr error
	for _, ip := range ips {
		conn, dialErr := dialer.DialContext(ctx, network, net.JoinHostPort(ip.Unmap().String(), port))
		if dialErr == nil {
			return conn, nil
		}
		lastErr = dialErr
	}
	if lastErr != nil {
		return nil, lastErr
	}
	return nil, errors.New("订阅主机连接失败")
}

func DefaultClientFactory(channel Channel, timeout float64) (*http.Client, error) {
	transport := &http.Transport{
		Proxy:               nil,
		MaxIdleConns:        8,
		IdleConnTimeout:     30 * time.Second,
		TLSHandshakeTimeout: 5 * time.Second,
	}
	if channel.Proxy != "" {
		u, err := url.Parse(channel.Proxy)
		if err != nil {
			return nil, fmt.Errorf("订阅通道代理地址无效：%s", channel.Proxy)
		}
		transport.Proxy = http.ProxyURL(u)
	} else if defaultDirectDialer != nil {
		transport.DialContext = defaultDirectDialer
	} else {
		transport.DialContext = safeDirectDialContext
	}
	return &http.Client{
		Transport: transport,
		Timeout:   time.Duration(timeout * float64(time.Second)),
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}, nil
}

type FetchOptions struct {
	ClientFactory       func(channel Channel) (*http.Client, error)
	RequestTimeout      float64
	TotalTimeout        float64
	HedgeDelay          float64
	MaxParallelChannels int
	PreferredEgress     string
	EgressScores        map[string]float64
	ResponseParser      func(raw []byte, fmtStr string) ([]map[string]any, error)
	customClient        bool
}

func (o *FetchOptions) fill() {
	if o.ClientFactory != nil {
		o.customClient = true
	} else {
		o.ClientFactory = func(c Channel) (*http.Client, error) {
			return DefaultClientFactory(c, coalesceF(o.RequestTimeout, DefaultFetchTimeout))
		}
	}
	if o.RequestTimeout <= 0 {
		o.RequestTimeout = DefaultFetchTimeout
	}
	if o.TotalTimeout <= 0 {
		o.TotalTimeout = DefaultSubscriptionDeadline
	}
	if o.HedgeDelay <= 0 {
		o.HedgeDelay = DefaultHedgeDelay
	}
	if o.MaxParallelChannels <= 0 {
		o.MaxParallelChannels = DefaultFetchParallelism
	}
}

func coalesceF(v, def float64) float64 {
	if v <= 0 {
		return def
	}
	return v
}

type FetchResult struct {
	Raw                []byte
	HTTPStatus         int
	ContentType        string
	Channel            string
	Fmt                string
	Attempts           []FetchAttempt
	Userinfo           string
	ProfileTitle       string
	ContentDisposition string
	EgressID           string
	DurationMS         int
	ResolvedNodes      []map[string]any
}

type channelOutcome struct {
	result  *FetchResult
	attempt FetchAttempt
	issue   string
}

const maxRedirectHops = 3

var blockedRedirectPrefixes = []netip.Prefix{
	netip.MustParsePrefix("0.0.0.0/8"),
	netip.MustParsePrefix("100.64.0.0/10"),
	netip.MustParsePrefix("192.0.0.0/24"),
	netip.MustParsePrefix("192.0.2.0/24"),
	netip.MustParsePrefix("198.18.0.0/15"),
	netip.MustParsePrefix("198.51.100.0/24"),
	netip.MustParsePrefix("203.0.113.0/24"),
	netip.MustParsePrefix("240.0.0.0/4"),
	netip.MustParsePrefix("2001:db8::/32"),
	netip.MustParsePrefix("64:ff9b::/96"),
	netip.MustParsePrefix("64:ff9b:1::/48"),
	netip.MustParsePrefix("2002::/16"),
	netip.MustParsePrefix("2001::/32"),
}

func isPublicRedirectIP(ip netip.Addr) bool {
	ip = ip.Unmap()
	if !ip.IsGlobalUnicast() || ip.IsPrivate() || ip.IsLoopback() || ip.IsLinkLocalUnicast() || ip.IsMulticast() || ip.Zone() != "" {
		return false
	}
	for _, prefix := range blockedRedirectPrefixes {
		if prefix.Contains(ip) {
			return false
		}
	}
	return true
}

func assertHTTPURL(raw string) (*url.URL, error) {
	parsed, err := url.Parse(strings.TrimSpace(raw))
	if err != nil {
		return nil, &RedirectPolicyError{Reason: "bad_scheme"}
	}
	scheme := strings.ToLower(parsed.Scheme)
	if (scheme != "http" && scheme != "https") || parsed.Hostname() == "" {
		return nil, &RedirectPolicyError{Reason: "bad_scheme"}
	}
	return parsed, nil
}

func checkRedirectTarget(ctx context.Context, target *url.URL, viaProxy bool) error {
	scheme := strings.ToLower(target.Scheme)
	if (scheme != "http" && scheme != "https") || target.Hostname() == "" {
		return &RedirectPolicyError{Reason: "bad_scheme"}
	}
	host := strings.ToLower(target.Hostname())
	if strings.HasSuffix(host, ".") || host == "localhost" || strings.HasSuffix(host, ".localhost") ||
		strings.HasSuffix(host, ".local") || strings.HasSuffix(host, ".internal") {
		return &RedirectPolicyError{Reason: "private_ip"}
	}
	if ip, err := netip.ParseAddr(host); err == nil {
		if !isPublicRedirectIP(ip) {
			return &RedirectPolicyError{Reason: "private_ip"}
		}
		return nil
	}
	if !viaProxy && defaultDirectDialer == nil {
		ips, err := net.DefaultResolver.LookupNetIP(ctx, "ip", host)
		if err != nil || len(ips) == 0 {
			return &RedirectPolicyError{Reason: "dns_error"}
		}
		for _, ip := range ips {
			if !isPublicRedirectIP(ip) {
				return &RedirectPolicyError{Reason: "private_ip"}
			}
		}
	}
	return nil
}

func FetchSubscription(ctx context.Context, urlStr string, channels []Channel, opts FetchOptions) (*FetchResult, error) {
	if _, err := assertHTTPURL(urlStr); err != nil {
		return nil, err
	}
	opts.fill()
	ordered := make([]Channel, len(channels))
	copy(ordered, channels)
	if opts.PreferredEgress != "" || len(opts.EgressScores) > 0 {
		for i := 1; i < len(ordered); i++ {
			for j := i; j > 0 && channelLess(ordered[j], ordered[j-1], opts); j-- {
				ordered[j], ordered[j-1] = ordered[j-1], ordered[j]
			}
		}
	}
	if len(ordered) == 0 {
		return nil, &FetchFailedError{URL: urlStr}
	}

	started := time.Now()
	deadline := started.Add(time.Duration(opts.TotalTimeout * float64(time.Second)))
	fetchCtx, cancel := context.WithDeadline(ctx, deadline)
	defer cancel()

	var attempts []FetchAttempt
	var unsupported []string
	invalidEncoding := false
	emptyNodes := false
	nextIndex := 0
	type inflight struct {
		channel Channel
		outcome channelOutcome
	}
	active := map[*inflight]bool{}
	completed := make(chan *inflight, len(ordered))
	launch := func() {
		ch := ordered[nextIndex]
		nextIndex++
		f := &inflight{channel: ch}
		active[f] = true
		go func() {
			f.outcome = fetchOne(fetchCtx, urlStr, ch, opts)
			completed <- f
		}()
	}

	launch()
	for len(active) > 0 {
		remaining := time.Until(deadline)
		if remaining <= 0 || fetchCtx.Err() != nil {
			break
		}
		wait := remaining
		if nextIndex < len(ordered) && len(active) < opts.MaxParallelChannels {
			if d := time.Duration(opts.HedgeDelay * float64(time.Second)); d < wait {
				wait = d
			}
		}
		timer := time.NewTimer(wait)
		hedged := false
		var batch []*inflight
		select {
		case f := <-completed:
			batch = append(batch, f)
			for drained := true; drained; {
				drained = false
				select {
				case f := <-completed:
					batch = append(batch, f)
					drained = true
				default:
				}
			}
		case <-timer.C:
			hedged = true
		case <-fetchCtx.Done():
			for drained := true; drained; {
				drained = false
				select {
				case f := <-completed:
					batch = append(batch, f)
					drained = true
				default:
				}
			}
		}
		timer.Stop()
		if len(batch) == 0 {
			if fetchCtx.Err() != nil {
				break
			}
			if hedged && nextIndex < len(ordered) && len(active) < opts.MaxParallelChannels && time.Until(deadline) > 0 {
				launch()
			}
			continue
		}
		winner := (*FetchResult)(nil)
		for _, f := range batch {
			delete(active, f)
			outcome := f.outcome
			attempts = append(attempts, outcome.attempt)
			switch outcome.issue {
			case "":
			case "invalid_utf8":
				invalidEncoding = true
			case FormatEmpty:
				emptyNodes = true
			default:
				unsupported = append(unsupported, outcome.issue)
			}
			if outcome.result != nil && winner == nil {
				winner = outcome.result
			}
		}
		if winner != nil {
			winner.DurationMS = int(time.Since(started) / time.Millisecond)
			winner.Attempts = attempts
			return winner, nil
		}
		for fetchCtx.Err() == nil && nextIndex < len(ordered) && len(active) < opts.MaxParallelChannels {
			launch()
		}
	}

	if time.Now().After(deadline) || time.Until(deadline) <= 0 {
		for f := range active {
			a := FetchAttempt{
				Channel: f.channel.Name,
				Fmt:     FormatUnknown,
				Error:   "subscription_deadline",
				Stage:   "overall_timeout",
			}
			attempts = append(attempts, a)
		}
	} else if fetchCtx.Err() != nil {
		for f := range active {
			attempts = append(attempts, FetchAttempt{
				Channel: f.channel.Name,
				Fmt:     FormatUnknown,
				Error:   "context_canceled",
				Stage:   "canceled",
			})
		}
	}
	durationMS := int(time.Since(started) / time.Millisecond)
	if len(unsupported) > 0 {
		return nil, &UnsupportedFormatError{Format: unsupported[0], Attempts: attempts, DurationMS: durationMS}
	}
	if invalidEncoding {
		return nil, &InvalidEncodingError{Attempts: attempts, DurationMS: durationMS}
	}
	if emptyNodes {
		return nil, &EmptyNodeSetError{Format: FormatYAML, Attempts: attempts, DurationMS: durationMS}
	}
	return nil, &FetchFailedError{URL: urlStr, Attempts: attempts, DurationMS: durationMS}
}

func channelLess(a, b Channel, opts FetchOptions) bool {
	aPref := opts.PreferredEgress != "" && a.EgressID == opts.PreferredEgress
	bPref := opts.PreferredEgress != "" && b.EgressID == opts.PreferredEgress
	if aPref != bPref {
		return aPref
	}
	sa := opts.EgressScores[egressKeyOf(a)]
	sb := opts.EgressScores[egressKeyOf(b)]
	return sa > sb
}

func egressKeyOf(c Channel) string {
	if c.EgressID != "" {
		return c.EgressID
	}
	return c.Name
}

func fetchOne(ctx context.Context, urlStr string, channel Channel, opts FetchOptions) channelOutcome {
	channelStarted := time.Now()
	egressID := channel.EgressID
	if egressID == "" {
		egressID = channel.Name
	}
	fail := func(status *int, fmtStr, errMsg, stage string) channelOutcome {
		return channelOutcome{attempt: FetchAttempt{
			Channel: channel.Name, OK: false, HTTPStatus: status, Fmt: fmtStr,
			Error: errMsg, Stage: stage,
			ElapsedMS: int(time.Since(channelStarted) / time.Millisecond),
			EgressID:  egressID,
		}}
	}

	reqCtx, cancelReq := context.WithTimeout(ctx, time.Duration(opts.RequestTimeout*float64(time.Second)))
	defer cancelReq()
	client, err := opts.ClientFactory(channel)
	if err != nil {
		return fail(nil, FormatUnknown, "transport_error", "transport")
	}
	manualClient := *client
	manualClient.CheckRedirect = func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}

	target := urlStr
	var resp *http.Response
	for hops := 0; ; {
		req, reqErr := http.NewRequestWithContext(reqCtx, http.MethodGet, target, nil)
		if reqErr != nil {
			return fail(nil, FormatUnknown, "transport_error", "transport")
		}
		req.Header.Set("User-Agent", SUB_USER_AGENT)
		resp, err = manualClient.Do(req)
		if err != nil {
			var polErr *RedirectPolicyError
			var netErr net.Error
			switch {
			case errors.As(err, &polErr):
				return fail(nil, FormatUnknown, "provider_redirect_blocked: "+polErr.Error(), "policy")
			case errors.As(err, &netErr) && netErr.Timeout():
				return fail(nil, FormatUnknown, "request_timeout", "timeout")
			case isProxyError(err):
				return fail(nil, FormatUnknown, "ProxyError", "proxy")
			case isConnectError(err):
				return fail(nil, FormatUnknown, "ConnectError", "connect")
			}
			return fail(nil, FormatUnknown, "transport_error", "transport")
		}
		if resp.StatusCode >= 300 && resp.StatusCode < 400 {
			loc := strings.TrimSpace(resp.Header.Get("Location"))
			resp.Body.Close()
			if loc == "" {
				return fail(nil, FormatUnknown, "provider_redirect_blocked", "policy")
			}
			if hops >= maxRedirectHops {
				return fail(nil, FormatUnknown, "provider_redirect_blocked: 订阅地址重定向过多", "policy")
			}
			nextURL, parseErr := req.URL.Parse(loc)
			if parseErr != nil {
				return fail(nil, FormatUnknown, "provider_redirect_blocked: 订阅地址不支持（仅 http/https）", "policy")
			}
			if polErr := checkRedirectTarget(reqCtx, nextURL, opts.customClient || channel.Proxy != ""); polErr != nil {
				return fail(nil, FormatUnknown, "provider_redirect_blocked: "+polErr.Error(), "policy")
			}
			hops++
			target = nextURL.String()
			continue
		}
		break
	}
	defer resp.Body.Close()
	raw, readErr := readBody(resp)
	if readErr != nil {
		var tooLarge *SubscriptionTooLargeError
		if errors.As(readErr, &tooLarge) {
			return fail(nil, FormatUnknown, "provider_size_limit: "+tooLarge.Error(), "policy")
		}
		var netErr net.Error
		if errors.As(readErr, &netErr) && netErr.Timeout() {
			return fail(nil, FormatUnknown, "request_timeout", "timeout")
		}
		return fail(&resp.StatusCode, FormatUnknown, "transport_error", "transport")
	}
	elapsedMS := int(time.Since(channelStarted) / time.Millisecond)
	result, attempt, issue := validateFetchResponse(resp.StatusCode, resp.Header.Get("Content-Type"), raw,
		resp.Header.Get("subscription-userinfo"), resp.Header.Get("profile-title"), resp.Header.Get("content-disposition"),
		channel, elapsedMS, egressID, opts.ResponseParser)
	if attempt != nil {
		return channelOutcome{result: result, attempt: *attempt, issue: issue}
	}
	return channelOutcome{result: result, issue: issue}
}

func readBody(resp *http.Response) ([]byte, error) {
	const maxSubscriptionBytes = 20 << 20
	buf := make([]byte, 0, 4096)
	chunk := make([]byte, 8192)
	for {
		n, err := resp.Body.Read(chunk)
		if n > 0 {
			if len(buf)+n > maxSubscriptionBytes {
				return nil, &SubscriptionTooLargeError{}
			}
			buf = append(buf, chunk[:n]...)
		}
		if err != nil {
			if errors.Is(err, io.EOF) {
				return buf, nil
			}
			return buf, err
		}
	}
}

func isProxyError(err error) bool {
	return strings.Contains(err.Error(), "proxyconnect") ||
		strings.Contains(err.Error(), "proxy: unknown scheme") ||
		strings.Contains(err.Error(), "socks connect")
}

func isConnectError(err error) bool {
	var opErr *net.OpError
	if errors.As(err, &opErr) {
		return opErr.Op == "dial"
	}
	return false
}

func validateFetchResponse(statusCode int, contentType string, raw []byte,
	userinfo, profileTitle, contentDisposition string,
	channel Channel, elapsedMS int, egressID string,
	responseParser func(raw []byte, fmtStr string) ([]map[string]any, error),
) (*FetchResult, *FetchAttempt, string) {
	att := func(ok bool, fmtStr, errMsg, stage string, issue string) (*FetchAttempt, string) {
		return &FetchAttempt{
			Channel: channel.Name, OK: ok, HTTPStatus: &statusCode, Fmt: fmtStr,
			Error: errMsg, Stage: stage, ElapsedMS: elapsedMS, EgressID: egressID,
		}, issue
	}
	if statusCode != 200 {
		a, _ := att(false, FormatUnknown, fmt.Sprintf("http_%d", statusCode), "http", "")
		return nil, a, ""
	}
	if len(raw) == 0 {
		a, issue := att(false, FormatEmpty, "empty_body", "content", "")
		return nil, a, issueOr(issue, FormatEmpty)
	}
	fmtStr, err := DetectFormat(raw)
	if err != nil {
		a, _ := att(false, FormatUnknown, "invalid UTF-8", "encoding", "")
		return nil, a, "invalid_utf8"
	}
	if fmtStr == FormatYAML {
		if _, perr := ParseNodes(raw, fmtStr); perr != nil {
			var empty *EmptyNodeSetError
			if errors.As(perr, &empty) {
				a, _ := att(false, fmtStr, "empty_node_set", "validation", "")
				return nil, a, FormatEmpty
			}
			a, _ := att(false, fmtStr, "invalid_subscription_yaml", "validation", "")
			return nil, a, fmtStr
		}
	} else if responseParser != nil && (fmtStr == FormatURI || fmtStr == FormatUnknown) {
		parsedNodes, perr := responseParser(raw, fmtStr)
		if perr != nil || len(parsedNodes) == 0 {
			a, _ := att(false, fmtStr, "invalid_subscription_content", "validation", "")
			return nil, a, "invalid_subscription_content"
		}
		a, _ := att(true, fmtStr, "", "complete", "")
		result := &FetchResult{
			Raw:                raw,
			HTTPStatus:         statusCode,
			ContentType:        contentType,
			Channel:            channel.Name,
			Fmt:                fmtStr,
			Userinfo:           userinfo,
			ProfileTitle:       profileTitle,
			ContentDisposition: contentDisposition,
			EgressID:           egressID,
			DurationMS:         elapsedMS,
			ResolvedNodes:      parsedNodes,
			Attempts:           []FetchAttempt{*a},
		}
		return result, nil, ""
	} else {
		a, _ := att(false, fmtStr, "unsupported_subscription_format", "validation", "")
		return nil, a, fmtStr
	}
	a, _ := att(true, fmtStr, "", "complete", "")
	nodes, _ := ParseNodes(raw, fmtStr)
	result := &FetchResult{
		Raw:                raw,
		HTTPStatus:         statusCode,
		ContentType:        contentType,
		Channel:            channel.Name,
		Fmt:                fmtStr,
		Userinfo:           userinfo,
		ProfileTitle:       profileTitle,
		ContentDisposition: contentDisposition,
		EgressID:           egressID,
		DurationMS:         elapsedMS,
		ResolvedNodes:      nodes,
	}
	return result, a, ""
}

func issueOr(current, def string) string {
	if current != "" {
		return current
	}
	return def
}

func BuildChannels(kernelProxy string, poolChannels []Channel) []Channel {
	channels := []Channel{{Name: ChannelDirect, EgressID: ChannelDirect}}
	if kernelProxy != "" {
		channels = append(channels, Channel{Name: ChannelKernel, Proxy: kernelProxy, EgressID: "runtime"})
	}
	channels = append(channels, poolChannels...)
	return channels
}
