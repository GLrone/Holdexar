package proxypool

// 订阅抓取：有限竞速，返回第一份格式和节点内容都有效的订阅。
// 网络请求全部发生在写闸之外；任何失败都保留最后一次成功快照。

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"
)

// SUB_USER_AGENT 必须与内核拉取 provider 时同款：面板按 UA 分流订阅格式，
// 不含 clash 关键字会回落 base64 节点表，个别机场对非 clash UA 直接 404。
// 版本对齐 kernel_release.MIHOMO_VERSION 单一来源。
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

// Channel 一条抓取通道。Proxy 为空即直连。
type Channel struct {
	Name     string
	Proxy    string
	EgressID string
}

// ── 错误类型（稳定、可断言）──────────────────────────────────────

// SnapshotError 订阅快照失败基类语义：不改 Registry、不覆盖旧快照。
type SnapshotError struct{ msg string }

func (e *SnapshotError) Error() string { return e.msg }

// FetchFailedError 所有通道都没拿到可判定内容。
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

// UnsupportedFormatError 内容拿到了但格式不归本模块处理。
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
	}
	return fmt.Sprintf("不支持的订阅格式：%s", e.Format)
}

// EmptyNodeSetError 格式合法但解析出 0 个节点——按失败处理。
type EmptyNodeSetError struct {
	Format     string
	Attempts   []FetchAttempt
	DurationMS int
}

func (e *EmptyNodeSetError) Error() string {
	return fmt.Sprintf("订阅解析出 0 个节点（%s），按失败处理", e.Format)
}

// InvalidEncodingError 正文解不出 UTF-8（劫持页/非 UTF-8），不洗白。
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

// ParseNodes inline YAML → 节点列表；其余格式一律不支持（与 detect_format
// 同一套严格解码，解析入口不得成为非法字节洗白的第二条路）。
func ParseNodes(raw []byte, fmtStr string) ([]map[string]any, error) {
	if fmtStr != FormatYAML {
		return nil, &UnsupportedFormatError{Format: fmtStr}
	}
	text, err := decodeUTF8Strict(raw)
	if err != nil {
		return nil, &InvalidEncodingError{}
	}
	doc, err := clashYAMLLoad(text)
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

// ── 格式判定 ──────────────────────────────────────────────────────

var (
	reProvidersKey = regexp.MustCompile(`(?m)^proxy-providers[\t\n\v\f\r \x1c-\x1f\x85\xa0\p{Zs}]*:`)
	reProxiesKey   = regexp.MustCompile(`(?m)^proxies[\t\n\v\f\r \x1c-\x1f\x85\xa0\p{Zs}]*:`)
	reURIScheme    = regexp.MustCompile(`^[a-z0-9+.\-]+://`)
	reBase64Body   = regexp.MustCompile(`^[A-Za-z0-9+/=]+$`)
)

// DetectFormat 判定订阅正文格式。只判定不解析；非合法 UTF-8 报错。
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

// ── 出口竞速 ──────────────────────────────────────────────────────

// ClientFactory 按通道产出 HTTP 客户端（生产：带通道代理的 Transport；测试：httptest）。
type ClientFactory func(channel Channel) (*http.Client, error)

// DefaultClientFactory 生产默认：按通道的 proxy 建客户端（连接 5 秒上限）。
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
	}
	return &http.Client{
		Transport: transport,
		Timeout:   time.Duration(timeout * float64(time.Second)),
	}, nil
}

// FetchOptions 竞速参数（缺省即生产标定）。
type FetchOptions struct {
	ClientFactory       func(channel Channel) (*http.Client, error)
	RequestTimeout      float64
	TotalTimeout        float64
	HedgeDelay          float64
	MaxParallelChannels int
	PreferredEgress     string
	EgressScores        map[string]float64
}

func (o *FetchOptions) fill() {
	if o.ClientFactory == nil {
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

// FetchResult 获胜通道的产物（校验通过才产生）。
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
	issue   string // ""=成功；否则失败归类键
}

// FetchSubscription 有限竞速抓取订阅，第一份校验通过的内容即获胜。
func FetchSubscription(ctx context.Context, urlStr string, channels []Channel, opts FetchOptions) (*FetchResult, error) {
	opts.fill()
	ordered := make([]Channel, len(channels))
	copy(ordered, channels)
	if opts.PreferredEgress != "" || len(opts.EgressScores) > 0 {
		// 稳定排序：首选出口优先，其余按历史成功率降序
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

	defer cancel()

	launch()
	for len(active) > 0 {
		remaining := time.Until(deadline)
		if remaining <= 0 {
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
			// 排空同一波完成的其余通道（Python FIRST_COMPLETED 的 done 集合）
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
			if hedged && nextIndex < len(ordered) && len(active) < opts.MaxParallelChannels && time.Until(deadline) > 0 {
				launch()
			}
			continue
		}
		// 完成批内第一份有效内容即获胜（Python 同：winner 按完成时间取首个非空结果）
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
		// 全败：立即补位（有别于 hedge 等待）
		for nextIndex < len(ordered) && len(active) < opts.MaxParallelChannels {
			launch()
		}
	}

	// deadline 到：在飞通道记 subscription_deadline 留痕
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

// fetchOne 单通道：下载 → 内容校验（网络等待全部在该函数内，调用方闸外）。
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
	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, urlStr, nil)
	if err != nil {
		return fail(nil, FormatUnknown, "transport_error", "transport")
	}
	req.Header.Set("User-Agent", SUB_USER_AGENT)
	resp, err := client.Do(req)
	if err != nil {
		var netErr net.Error
		switch {
		case errors.As(err, &netErr) && netErr.Timeout():
			return fail(nil, FormatUnknown, "request_timeout", "timeout")
		case isProxyError(err):
			return fail(nil, FormatUnknown, "ProxyError", "proxy")
		case isConnectError(err):
			return fail(nil, FormatUnknown, "ConnectError", "connect")
		}
		return fail(nil, FormatUnknown, "transport_error", "transport")
	}
	defer resp.Body.Close()
	raw, readErr := readBody(resp)
	if readErr != nil {
		var netErr net.Error
		if errors.As(readErr, &netErr) && netErr.Timeout() {
			return fail(nil, FormatUnknown, "request_timeout", "timeout")
		}
		return fail(&resp.StatusCode, FormatUnknown, "transport_error", "transport")
	}
	elapsedMS := int(time.Since(channelStarted) / time.Millisecond)
	result, attempt, issue := validateFetchResponse(resp.StatusCode, resp.Header.Get("Content-Type"), raw,
		resp.Header.Get("subscription-userinfo"), resp.Header.Get("profile-title"), resp.Header.Get("content-disposition"),
		channel, elapsedMS, egressID)
	if attempt != nil {
		return channelOutcome{result: result, attempt: *attempt, issue: issue}
	}
	return channelOutcome{result: result, issue: issue}
}

func readBody(resp *http.Response) ([]byte, error) {
	const maxSubscriptionBytes = 8 << 20
	buf := make([]byte, 0, 4096)
	chunk := make([]byte, 8192)
	for {
		n, err := resp.Body.Read(chunk)
		if n > 0 {
			if len(buf)+n > maxSubscriptionBytes {
				return nil, errors.New("订阅正文超过大小上限")
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

// validateFetchResponse 内容校验链（对齐 Python _validate_fetch_response
// 的生产主链：仅 inline YAML 有效；provider_yaml 解析随后续批次迁移）。
func validateFetchResponse(statusCode int, contentType string, raw []byte,
	userinfo, profileTitle, contentDisposition string,
	channel Channel, elapsedMS int, egressID string,
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

// BuildChannels 直连 → 内核 → 池 lane → 本机混合端口（kernel/池/本机通道
// 的供给面随内核批次接线，本批调用方显式传通道）。
func BuildChannels(kernelProxy string, poolChannels []Channel) []Channel {
	channels := []Channel{{Name: ChannelDirect, EgressID: ChannelDirect}}
	if kernelProxy != "" {
		channels = append(channels, Channel{Name: ChannelKernel, Proxy: kernelProxy, EgressID: "runtime"})
	}
	channels = append(channels, poolChannels...)
	return channels
}
