package proxypool

// L1 出口身份 / L2 业务准入探测与落账（Python health.py 对齐）。
// 分层铁律：L1/L2 只记录观测，不擦已有出口身份、不碰 L0 时间戳；只有 L2 的
// 有效业务结论经状态机推进节点状态（L0/L1 仅诊断）。目标服务故障（ipify/Steam
// 侧失败）不得变成节点失败——否则外部服务健康状态会决定代理池健康。
// 落账边界：探测（网络）在写闸外完成，每波结论一次短提交进闸；闸内回调失败
// 由写闸整体回滚——健康、观测与冷却要么同事务落成，要么什么都不写。

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/netip"
	"net/url"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"holdexar/server-go/internal/clashcfg"
	"holdexar/server-go/internal/store"
)

// readFileText 读配置文本；不存在返回空串。
func readFileText(path string) (string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	return string(raw), nil
}

// parseHTTPProxyURL 本机 listener 代理入口。
func parseHTTPProxyURL(port int) (*url.URL, error) {
	return url.Parse(fmt.Sprintf("http://127.0.0.1:%d", port))
}

const (
	ProbeLevelL1 = "L1"
	ProbeLevelL2 = "L2"

	// 生产默认公网 IP 回显服务（可覆盖）；测试换本地目标。
	ExitIPTargetURL   = "https://api.ipify.org?format=json"
	DefaultL1Timeout  = 15 * time.Second
	DefaultL2Timeout  = 30 * time.Second
	ProbeCommitEvery  = 10
	ProbeConcurrency  = 20
	TunnelFailureStat = 502

	// 失败归因枚举：L1 必须能区分「节点/选择失败」与「目标服务故障」。
	NodeProbeFailed    = "NODE_PROBE_FAILED"
	NodeTunnelFailed   = "NODE_TUNNEL_FAILED"
	TargetServiceFail  = "TARGET_SERVICE_FAILED"
	UncertainFailure   = "UNCERTAIN_FAILURE"
	InvalidIPResponse  = "INVALID_IP_RESPONSE"
	BusinessOK         = "BUSINESS_OK"
	InvalidBizResponse = "INVALID_BUSINESS_RESPONSE"

	// 与 crawler 生产探测同一 appid / 区服（Half-Life 2，长期在售）。
	DefaultBusinessAppID = 220
	BusinessCC           = "us"

	// 手动体检观测明细（HealthObservation.detail 固定枚举，不放自由文本）。
	DetailSteamOK          = "CLASH_STEAM_OK"
	DetailSteamHTTPError   = "CLASH_STEAM_HTTP_ERROR"
	DetailSteamHTTP502     = "CLASH_STEAM_HTTP_502"
	DetailSteamHTTP504     = "CLASH_STEAM_HTTP_504"
	DetailSteamInvalid     = "CLASH_STEAM_INVALID_RESPONSE"
	DetailSteamTransport   = "CLASH_STEAM_TRANSPORT_FAILURE"
	DetailExitIP           = "CLASH_EXIT_IP"
	DetailRecoveryOK       = "SELF_HOSTED_RECOVERY_OK"
	DefaultIngestChannel   = "manual"
	DefaultScheduleChannel = "scheduled"
)

// browseProbeURL 健康探针主机（生产 StoreBrowse 同主机同路径）。
const browseProbeURL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"

// storeBrowseSafeChars urlencode(safe=...) 白名单：字母数字恒安全之外追加集合
// （对齐 Python _URL_SAFE 与 quote 的恒安全字符表）。
const storeBrowseSafeChars = `{}",:~-_.`

// BusinessProbeURL L2 业务目标：直接复用 crawler 的 StoreBrowse 契约
// （单 appid、不带 extras 的最小负载；不另建第二套 URL）。
func BusinessProbeURL(appID int64, cc string) string {
	if cc == "" {
		cc = BusinessCC
	}
	body := fmt.Sprintf(`{"ids":[{"appid":%d}],"context":{"language":"english",`+
		`"country_code":"%s","steam_realm":1},"data_request":`+
		`{"include_basic_info":true,"include_all_purchase_options":true}}`, appID, strings.ToUpper(cc))
	return browseProbeURL + "?input_json=" + storeBrowseQuote(body)
}

// storeBrowseQuote Python urllib quote 语义：ASCII 字母数字与恒安全字符之外，
// safe 白名单内原样、其余按 %XX 大写十六进制。
func storeBrowseQuote(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c >= 'a' && c <= 'z', c >= 'A' && c <= 'Z', c >= '0' && c <= '9',
			c == '_' || c == '.' || c == '-' || c == '~':
			b.WriteByte(c)
		case strings.IndexByte(storeBrowseSafeChars, c) >= 0:
			b.WriteByte(c)
		default:
			fmt.Fprintf(&b, "%%%02X", c)
		}
	}
	return b.String()
}

// ExitIPResult 一次 L1 出口 IP 探测的原始结论。
type ExitIPResult struct {
	RuntimeName string
	OK          bool
	ExitIP      string
	Detail      string
	LatencyMS   int64
}

// BusinessResult 一次 L2 业务探测的原始结论。
type BusinessResult struct {
	RuntimeName     string
	OK              bool
	HTTPStatus      int
	FinalPriceCents int64
	Detail          string
	LatencyMS       int64
	Attribution     string
}

// selectSelector 把 selector（GLOBAL 或探测 lane 组）切到目标节点并回读归因：
// 回读 now 不是目标节点时，接下来测到的一切都不属于它——宁可记失败。
// L1 与 L2 共用这一步。成功返回空串。
func selectSelector(ctx context.Context, controllerURL, secret, selector, runtimeName string, timeout time.Duration) string {
	client := probeHTTPClient(timeout)
	doJSON := func(method, path string, body string) (int, []byte, error) {
		var reader *strings.Reader
		if body != "" {
			reader = strings.NewReader(body)
		} else {
			reader = strings.NewReader("")
		}
		req, err := http.NewRequestWithContext(ctx, method, strings.TrimRight(controllerURL, "/")+path, reader)
		if err != nil {
			return 0, nil, err
		}
		req.Header.Set("Content-Type", "application/json")
		if secret != "" {
			req.Header.Set("Authorization", "Bearer "+secret)
		}
		resp, err := client.Do(req)
		if err != nil {
			return 0, nil, err
		}
		defer resp.Body.Close()
		raw := make([]byte, 0, 4096)
		buf := make([]byte, 1024)
		for {
			n, rerr := resp.Body.Read(buf)
			raw = append(raw, buf[:n]...)
			if rerr != nil {
				break
			}
			if len(raw) > 1<<20 {
				break
			}
		}
		return resp.StatusCode, raw, nil
	}
	status, _, err := doJSON(http.MethodPut,
		"/proxies/"+pyURLPathEscape(selector), fmt.Sprintf(`{"name":%q}`, runtimeName))
	if err != nil {
		return fmt.Sprintf("%s: 控制器不可达（%T）", NodeProbeFailed, err)
	}
	if status >= 400 {
		return fmt.Sprintf("%s: 选择节点失败（HTTP %d）", NodeProbeFailed, status)
	}
	_, raw, err := doJSON(http.MethodGet, "/proxies/"+pyURLPathEscape(selector), "")
	if err != nil {
		return fmt.Sprintf("%s: 控制器不可达（%T）", NodeProbeFailed, err)
	}
	var payload struct {
		Now string `json:"now"`
	}
	if json.Unmarshal(raw, &payload) != nil {
		return fmt.Sprintf("%s: 控制器响应不是 JSON", NodeProbeFailed)
	}
	if payload.Now != runtimeName {
		return fmt.Sprintf("%s: %s 的 now=%q 不是 %q", NodeProbeFailed, selector, payload.Now, runtimeName)
	}
	return ""
}

// pyURLPathEscape Python quote(name, safe="") 语义：斜杠与保留字符一律编码。
func pyURLPathEscape(s string) string {
	return url.PathEscape(s)
}

// requestViaPort 经本机 listener 端口发一次真实 GET。网络失败 status=0。
func requestViaPort(ctx context.Context, port int, target string, timeout time.Duration) (int, string, int64, string) {
	proxyURL, err := parseHTTPProxyURL(port)
	if err != nil {
		return 0, "", 0, "ConfigError"
	}
	client := &http.Client{
		Timeout: timeout,
		Transport: &http.Transport{
			Proxy:                 http.ProxyURL(proxyURL),
			TLSHandshakeTimeout:   timeout,
			ResponseHeaderTimeout: timeout,
		},
	}
	started := time.Now()
	req, rerr := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
	if rerr != nil {
		return 0, "", 0, "InvalidURL"
	}
	resp, err := client.Do(req)
	if err != nil {
		return 0, "", 0, fmt.Sprintf("%T", err)
	}
	defer resp.Body.Close()
	raw := make([]byte, 0, 8192)
	buf := make([]byte, 4096)
	for {
		n, rerr := resp.Body.Read(buf)
		raw = append(raw, buf[:n]...)
		if rerr != nil || len(raw) > 4<<20 {
			break
		}
	}
	return resp.StatusCode, string(raw), time.Since(started).Milliseconds(), ""
}

// ExtractIP 从回显响应取出口 IP：兼容 JSON（ipify format=json）与纯文本，
// 非法地址返回空串。
func ExtractIP(text string) string {
	candidate := pyTrimSpace(text)
	if strings.HasPrefix(candidate, "{") {
		var payload map[string]any
		if json.Unmarshal([]byte(candidate), &payload) != nil {
			return ""
		}
		candidate = pyTrimSpace(pyStr(payload["ip"]))
	}
	if candidate == "" {
		return ""
	}
	addr, err := netip.ParseAddr(candidate)
	if err != nil {
		return ""
	}
	return addr.String()
}

// AttributeGateway502 区分上游业务返回 502 与本地内核代理入口无法建隧的空体 502：
// 有响应体 = 目标服务；无响应体回探一次控制器 /delay 判节点隧道成败。
func AttributeGateway502(ctx context.Context, controllerURL, secret, runtimeName, targetURL, body string, timeout time.Duration) (string, string) {
	if pyTrimSpace(body) != "" {
		return fmt.Sprintf("%s: HTTP %d", TargetServiceFail, TunnelFailureStat), "target"
	}
	controlTimeoutMS := int(timeout.Milliseconds())
	if controlTimeoutMS < DefaultProbeTimeoutMS {
		controlTimeoutMS = DefaultProbeTimeoutMS
	}
	if controlTimeoutMS > 5000 {
		controlTimeoutMS = 5000
	}
	controlWait := timeout
	if minWait := time.Duration(controlTimeoutMS)*time.Millisecond + 2*time.Second; controlWait > minWait {
		controlWait = minWait
	}
	control := ProbeNode(ctx, controllerURL, secret, runtimeName, ProbeOptions{
		URL: targetURL, TimeoutMS: controlTimeoutMS, ClientTimeout: controlWait,
	})
	if control.OK {
		return fmt.Sprintf("%s: HTTP %d", TargetServiceFail, TunnelFailureStat), "target"
	}
	if control.Valid {
		return fmt.Sprintf("%s: HTTP %d（%s）", NodeTunnelFailed, TunnelFailureStat, control.Detail), "node"
	}
	return fmt.Sprintf("%s: HTTP %d（%s）", UncertainFailure, TunnelFailureStat, control.Detail), "uncertain"
}

// ExitProbeOptions L1 探测参数。
type ExitProbeOptions struct {
	Selector string // 缺省 GLOBAL
	URL      string // 缺省公网回显服务
	Timeout  time.Duration
}

// ProbeExitIP 选中节点并经其 listener 探出口身份；共享 selector 的调用必须串行。
func ProbeExitIP(ctx context.Context, controllerURL, secret string, mixedPort int, runtimeName string, opts ExitProbeOptions) ExitIPResult {
	selector := opts.Selector
	if selector == "" {
		selector = "GLOBAL"
	}
	target := opts.URL
	if target == "" {
		target = ExitIPTargetURL
	}
	timeout := opts.Timeout
	if timeout <= 0 {
		timeout = DefaultL1Timeout
	}
	if failure := selectSelector(ctx, controllerURL, secret, selector, runtimeName, timeout); failure != "" {
		return ExitIPResult{RuntimeName: runtimeName, Detail: failure}
	}
	status, text, latencyMS, errType := requestViaPort(ctx, mixedPort, target, timeout)
	if status == 0 {
		return ExitIPResult{RuntimeName: runtimeName,
			Detail: fmt.Sprintf("%s: %s", TargetServiceFail, errType)}
	}
	if status != 200 {
		return ExitIPResult{RuntimeName: runtimeName,
			Detail: fmt.Sprintf("%s: HTTP %d", TargetServiceFail, status), LatencyMS: latencyMS}
	}
	exitIP := ExtractIP(text)
	if exitIP == "" {
		shown := text
		if len(shown) > 60 {
			shown = shown[:60]
		}
		return ExitIPResult{RuntimeName: runtimeName,
			Detail:    fmt.Sprintf("%s: %q", InvalidIPResponse, shown),
			LatencyMS: latencyMS}
	}
	return ExitIPResult{RuntimeName: runtimeName, OK: true, ExitIP: exitIP, LatencyMS: latencyMS}
}

// BusinessProbeOptions L2 探测参数。
type BusinessProbeOptions struct {
	Selector string
	URL      string // 缺省按 appid 拼 StoreBrowse 探针 URL
	AppID    int64
	Timeout  time.Duration
}

// ProbeBusiness 选中节点并经其 listener 做一次 StoreBrowse 业务探测。
func ProbeBusiness(ctx context.Context, controllerURL, secret string, mixedPort int, runtimeName string, opts BusinessProbeOptions) BusinessResult {
	appID := opts.AppID
	if appID == 0 {
		appID = DefaultBusinessAppID
	}
	target := opts.URL
	if target == "" {
		target = BusinessProbeURL(appID, BusinessCC)
	}
	timeout := opts.Timeout
	if timeout <= 0 {
		timeout = DefaultL2Timeout
	}
	selector := opts.Selector
	if selector == "" {
		selector = "GLOBAL"
	}
	if failure := selectSelector(ctx, controllerURL, secret, selector, runtimeName, timeout); failure != "" {
		return BusinessResult{RuntimeName: runtimeName, Detail: failure}
	}
	status, text, latencyMS, errType := requestViaPort(ctx, mixedPort, target, timeout)
	if status == 0 {
		return BusinessResult{RuntimeName: runtimeName,
			Detail: fmt.Sprintf("%s: %s", TargetServiceFail, errType)}
	}
	if status == TunnelFailureStat {
		detail, attr := AttributeGateway502(ctx, controllerURL, secret, runtimeName, target, text, timeout)
		return BusinessResult{RuntimeName: runtimeName, HTTPStatus: status,
			Detail: detail, LatencyMS: latencyMS, Attribution: attr}
	}
	if status != 200 {
		return BusinessResult{RuntimeName: runtimeName, HTTPStatus: status,
			Detail:    fmt.Sprintf("%s: HTTP %d", TargetServiceFail, status),
			LatencyMS: latencyMS, Attribution: "target"}
	}
	var payload any
	if json.Unmarshal([]byte(text), &payload) != nil {
		return BusinessResult{RuntimeName: runtimeName, HTTPStatus: status,
			Detail: InvalidBizResponse + ": 响应不是 JSON", LatencyMS: latencyMS}
	}
	ok, reason, final := ValidateBusiness(payload, appID)
	if !ok {
		return BusinessResult{RuntimeName: runtimeName, HTTPStatus: status,
			Detail: InvalidBizResponse + ": " + reason, LatencyMS: latencyMS}
	}
	return BusinessResult{RuntimeName: runtimeName, OK: true, HTTPStatus: status,
		FinalPriceCents: final,
		Detail:          fmt.Sprintf("%s appid=%d final_price_in_cents=%d", BusinessOK, appID, final),
		LatencyMS:       latencyMS}
}

// ValidateBusiness 按生产 StoreBrowse 契约判定业务语义：
// response 信封 → store_items 按 appid 命中 → success 是整数 1（布尔 true 属契约
// 不符，显式拒绝）→ best_purchase_option.final_price_in_cents 可按 _to_int 解析。
func ValidateBusiness(payload any, appID int64) (bool, string, int64) {
	root, ok := payload.(map[string]any)
	if !ok {
		return false, "响应不是 JSON 对象", 0
	}
	envelope, ok := root["response"].(map[string]any)
	if !ok {
		return false, "缺少 response 信封", 0
	}
	items, ok := envelope["store_items"].([]any)
	if !ok {
		return false, "store_items 不是 list", 0
	}
	var item map[string]any
	for _, candidate := range items {
		entry, ok := candidate.(map[string]any)
		if !ok {
			continue
		}
		if id, ok := pyToProbeInt(entry["appid"]); ok && id == appID {
			item = entry
			break
		}
		if id, ok := pyToProbeInt(entry["id"]); ok && id == appID {
			item = entry
			break
		}
	}
	if item == nil {
		return false, fmt.Sprintf("store_items 里没有 appid=%d", appID), 0
	}
	if _, isBool := item["success"].(bool); isBool {
		return false, fmt.Sprintf("success 不是整数 1（实际 %s）", pyReprValue(item["success"])), 0
	}
	success, ok := pyToProbeInt(item["success"])
	if !ok || success != 1 {
		return false, fmt.Sprintf("success 不是整数 1（实际 %s）", pyReprValue(item["success"])), 0
	}
	best, ok := item["best_purchase_option"].(map[string]any)
	if !ok {
		return false, "缺少 best_purchase_option", 0
	}
	final, ok := pyToProbeInt(best["final_price_in_cents"])
	if !ok {
		return false, fmt.Sprintf("final_price_in_cents 无法按 _to_int 语义解析（实际 %s）",
			pyReprValue(best["final_price_in_cents"])), 0
	}
	return true, "", final
}

// ── 落账编排：波内并发探测（网络段在闸外），波末一次短提交进闸 ─────────────

// HealthChecker 三层体检编排器：持有库、写闸与运行时端点。
// names 由调用方给出（池文件读取归池构建层，本包不做）。
type HealthChecker struct {
	DB            *sql.DB
	Gate          *store.WriteGate
	DataDir       string
	ControllerURL string
	Secret        string
	ConfigPath    string
	Now           func() time.Time
}

func (h *HealthChecker) now() time.Time {
	if h.Now != nil {
		return h.Now()
	}
	return time.Now()
}

// Lanes 运行配置里端口在听的探测 lane。
func (h *HealthChecker) Lanes() []clashcfg.ProbeLane {
	raw, err := readFileText(h.ConfigPath)
	if err != nil || raw == "" {
		return nil
	}
	return PoolProbeLanes(raw)
}

// MixedPort 运行配置的混合端口（无配置返回 0，探针自会失败）。
func (h *HealthChecker) MixedPort() int {
	raw, err := readFileText(h.ConfigPath)
	if err != nil || raw == "" {
		return 0
	}
	return clashcfg.ParseMixedPort(raw)
}

// nodeRow 落账所需的节点行投影。
type nodeRow struct {
	ID                  int64
	NodeID              string
	RuntimeName         string
	State               string
	SubscriptionID      sql.NullInt64
	ConsecutiveFailures int64
	ExitIP              sql.NullString
	CooldownUntil       sql.NullString
}

func loadNodeRowsByRuntime(ctx context.Context, q queryable, names []string) (map[string]nodeRow, error) {
	if len(names) == 0 {
		return map[string]nodeRow{}, nil
	}
	out := map[string]nodeRow{}
	seen := map[string]bool{}
	var uniq []string
	for _, n := range names {
		if !seen[n] {
			seen[n] = true
			uniq = append(uniq, n)
		}
	}
	sort.Strings(uniq)
	query := `SELECT id, node_id, runtime_name, state, subscription_id, consecutive_failures, exit_ip, CAST(cooldown_until AS TEXT)
		FROM proxy_nodes WHERE runtime_name IN (` + placeholders(len(uniq)) + `)`
	args := make([]any, len(uniq))
	for i, n := range uniq {
		args[i] = n
	}
	rows, err := q.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var r nodeRow
		if err := rows.Scan(&r.ID, &r.NodeID, &r.RuntimeName, &r.State, &r.SubscriptionID,
			&r.ConsecutiveFailures, &r.ExitIP, &r.CooldownUntil); err != nil {
			return nil, err
		}
		out[r.RuntimeName] = r
	}
	return out, rows.Err()
}

func loadNodeRowsByID(ctx context.Context, q queryable, ids []string) (map[string]nodeRow, error) {
	if len(ids) == 0 {
		return map[string]nodeRow{}, nil
	}
	sort.Strings(ids)
	query := `SELECT id, node_id, runtime_name, state, subscription_id, consecutive_failures, exit_ip, CAST(cooldown_until AS TEXT)
		FROM proxy_nodes WHERE node_id IN (` + placeholders(len(ids)) + `)`
	args := make([]any, len(ids))
	for i, id := range ids {
		args[i] = id
	}
	rows, err := q.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]nodeRow{}
	for rows.Next() {
		var r nodeRow
		if err := rows.Scan(&r.ID, &r.NodeID, &r.RuntimeName, &r.State, &r.SubscriptionID,
			&r.ConsecutiveFailures, &r.ExitIP, &r.CooldownUntil); err != nil {
			return nil, err
		}
		out[r.NodeID] = r
	}
	return out, rows.Err()
}

func placeholders(n int) string {
	return strings.TrimSuffix(strings.Repeat("?,", n), ",")
}

type queryable interface {
	QueryContext(ctx context.Context, query string, args ...any) (*sql.Rows, error)
}

// insertObservation 写一条健康观测流水。
func insertObservation(ctx context.Context, exec interface {
	ExecContext(ctx context.Context, query string, args ...any) (sql.Result, error)
}, obs HealthObservationRow) error {
	_, err := exec.ExecContext(ctx, `INSERT INTO health_observations
		(node_id, level, ok, latency_ms, detail, observed_at, run_id, channel, target)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		obs.NodeID, obs.Level, boolInt(obs.OK), nullInt64Arg(obs.LatencyMS),
		nullTextArg(obs.Detail), pyTimeText(obs.ObservedAt), nullInt64Arg(obs.RunID),
		nullTextArg(obs.Channel), nullTextArg(obs.Target))
	return err
}

// HealthObservationRow 健康观测流水的写入形状。
type HealthObservationRow struct {
	NodeID     string
	Level      string
	OK         bool
	LatencyMS  int64
	Detail     string
	ObservedAt time.Time
	RunID      int64
	Channel    string
	Target     string
}

func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

func nullInt64Arg(v int64) any {
	if v == 0 {
		return nil
	}
	return v
}

func nullTextArg(s string) any {
	if s == "" {
		return nil
	}
	return s
}

// ExitIPOutcome 一波 L1 结论的账目（供调用方记录/断言）。
type ExitIPOutcome struct {
	NodeID      string
	RuntimeName string
	OK          bool
	ExitIP      string
	Detail      string
	LatencyMS   int64
}

// ExitIPCheckPool 对池内节点做一轮 L1 出口身份观测：lane 在位时按 lane 分波并发
// （每条 lane 一个独立组选择），无 lane 退回 GLOBAL 串行路径并按块提交。
// 成功 → exit_ip + last_l1_at；失败 → 只落观测与 detail，不动 exit_ip、不动状态。
func (h *HealthChecker) ExitIPCheckPool(ctx context.Context, names []string, opts ExitProbeOptions) ([]ExitIPOutcome, error) {
	lanes := h.Lanes()
	mixedPort := 0
	if len(lanes) == 0 {
		mixedPort = h.MixedPort()
	}
	now := h.now()
	byName, err := loadNodeRowsByRuntime(ctx, h.DB, names)
	if err != nil {
		return nil, err
	}
	target := opts.URL
	if target == "" {
		target = ExitIPTargetURL
	}
	var outcomes []ExitIPOutcome
	waveSize := len(lanes)
	if waveSize == 0 {
		waveSize = ProbeCommitEvery
	}
	for start := 0; start < len(names); start += waveSize {
		end := start + waveSize
		if end > len(names) {
			end = len(names)
		}
		chunk := names[start:end]
		type waveResult struct {
			name   string
			result ExitIPResult
		}
		results := make([]waveResult, len(chunk))
		if len(lanes) > 0 {
			var wg sync.WaitGroup
			for i, name := range chunk {
				if _, ok := byName[name]; !ok {
					continue
				}
				lane := lanes[i%len(lanes)]
				wg.Add(1)
				go func(i int, name string, lane clashcfg.ProbeLane) {
					defer wg.Done()
					results[i] = waveResult{name, ProbeExitIP(ctx, h.ControllerURL, h.Secret,
						lane.Port, name, ExitProbeOptions{Selector: lane.Group, URL: opts.URL, Timeout: opts.Timeout})}
				}(i, name, lane)
			}
			wg.Wait()
		} else {
			for i, name := range chunk { // 严格串行（GLOBAL 兜底路径）
				if _, ok := byName[name]; !ok {
					continue
				}
				results[i] = waveResult{name, ProbeExitIP(ctx, h.ControllerURL, h.Secret,
					mixedPort, name, ExitProbeOptions{URL: opts.URL, Timeout: opts.Timeout})}
			}
		}
		probed := make([]struct {
			row    nodeRow
			result ExitIPResult
		}, 0, len(chunk))
		for _, r := range results {
			if r.name == "" {
				continue
			}
			row, ok := byName[r.name]
			if !ok {
				continue
			}
			probed = append(probed, struct {
				row    nodeRow
				result ExitIPResult
			}{row, r.result})
			outcomes = append(outcomes, ExitIPOutcome{
				NodeID: row.NodeID, RuntimeName: r.result.RuntimeName, OK: r.result.OK,
				ExitIP: r.result.ExitIP, Detail: r.result.Detail, LatencyMS: r.result.LatencyMS,
			})
		}
		if err := h.commitExitIPWave(ctx, probed, now, target); err != nil {
			return outcomes, err
		}
	}
	return outcomes, nil
}

// commitExitIPWave 一波 L1 结论落账：闸内按主键重读（防丢失更新），短提交。
func (h *HealthChecker) commitExitIPWave(ctx context.Context, probed []struct {
	row    nodeRow
	result ExitIPResult
}, now time.Time, target string) error {
	if len(probed) == 0 {
		return nil
	}
	return h.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		if err := EnsureHealthSchema(ctx, conn); err != nil {
			return err
		}
		ids := make([]string, len(probed))
		for i, p := range probed {
			ids[i] = p.row.NodeID
		}
		fresh, err := loadNodeRowsByID(ctx, conn, ids)
		if err != nil {
			return err
		}
		for _, p := range probed {
			row, ok := fresh[p.row.NodeID]
			if !ok {
				continue
			}
			if p.result.OK && p.result.ExitIP != "" {
				if _, err := conn.ExecContext(ctx,
					`UPDATE proxy_nodes SET exit_ip = ?, last_l1_at = ? WHERE id = ?`,
					p.result.ExitIP, pyTimeText(now), row.ID); err != nil {
					return err
				}
			}
			if err := insertObservation(ctx, conn, HealthObservationRow{
				NodeID: row.NodeID, Level: ProbeLevelL1, OK: p.result.OK,
				LatencyMS: p.result.LatencyMS, Detail: p.result.Detail,
				ObservedAt: now, Channel: DefaultScheduleChannel, Target: target,
			}); err != nil {
				return err
			}
		}
		return nil
	})
}

// BusinessOutcome 一波 L2 结论的账目。
type BusinessOutcome struct {
	NodeID          string
	RuntimeName     string
	OK              bool
	HTTPStatus      int
	FinalPriceCents int64
	Detail          string
	LatencyMS       int64
	Attribution     string
	State           string
}

// BusinessCheckPool 对池内节点做一轮 L2 业务观测——节点生死判据的唯一定时来源。
// 只有有效业务响应能晋升；目标服务错误或归因不确定不罚节点；本地代理入口建隧
// 失败与请求超时/连不上计入节点失败证据（连续 3 次达退休线，状态机判定）。
func (h *HealthChecker) BusinessCheckPool(ctx context.Context, names []string, opts BusinessProbeOptions) ([]BusinessOutcome, error) {
	lanes := h.Lanes()
	mixedPort := 0
	if len(lanes) == 0 {
		mixedPort = h.MixedPort()
	}
	now := h.now()
	byName, err := loadNodeRowsByRuntime(ctx, h.DB, names)
	if err != nil {
		return nil, err
	}
	target := opts.URL
	if target == "" {
		appID := opts.AppID
		if appID == 0 {
			appID = DefaultBusinessAppID
		}
		target = BusinessProbeURL(appID, BusinessCC)
	}
	var outcomes []BusinessOutcome
	waveSize := len(lanes)
	if waveSize == 0 {
		waveSize = ProbeCommitEvery
	}
	for start := 0; start < len(names); start += waveSize {
		end := start + waveSize
		if end > len(names) {
			end = len(names)
		}
		chunk := names[start:end]
		type waveResult struct {
			name   string
			result BusinessResult
		}
		results := make([]waveResult, len(chunk))
		if len(lanes) > 0 {
			var wg sync.WaitGroup
			for i, name := range chunk {
				if _, ok := byName[name]; !ok {
					continue
				}
				lane := lanes[i%len(lanes)]
				wg.Add(1)
				go func(i int, name string, lane clashcfg.ProbeLane) {
					defer wg.Done()
					results[i] = waveResult{name, ProbeBusiness(ctx, h.ControllerURL, h.Secret,
						lane.Port, name, BusinessProbeOptions{Selector: lane.Group, URL: opts.URL,
							AppID: opts.AppID, Timeout: opts.Timeout})}
				}(i, name, lane)
			}
			wg.Wait()
		} else {
			for i, name := range chunk { // 严格串行（GLOBAL 兜底路径）
				if _, ok := byName[name]; !ok {
					continue
				}
				results[i] = waveResult{name, ProbeBusiness(ctx, h.ControllerURL, h.Secret,
					mixedPort, name, BusinessProbeOptions{URL: opts.URL, AppID: opts.AppID, Timeout: opts.Timeout})}
			}
		}
		probed := make([]struct {
			row    nodeRow
			result BusinessResult
		}, 0, len(chunk))
		for _, r := range results {
			if r.name == "" {
				continue
			}
			row, ok := byName[r.name]
			if !ok {
				continue
			}
			probed = append(probed, struct {
				row    nodeRow
				result BusinessResult
			}{row, r.result})
		}
		waveOutcomes, err := h.commitBusinessWave(ctx, probed, now, target)
		if err != nil {
			return outcomes, err
		}
		outcomes = append(outcomes, waveOutcomes...)
	}
	return outcomes, nil
}

// businessProbeOK 失败结论 → 状态机输入：true=成功晋升 / false=节点失败证据 /
// nil=不罚节点（目标服务故障、契约不符、归因不确定）。
func businessProbeOK(result BusinessResult) (bool, bool) {
	if result.OK {
		return true, true
	}
	switch {
	case result.Attribution == "node" || strings.HasPrefix(result.Detail, NodeTunnelFailed):
		return false, true
	case result.Attribution == "target" || result.Attribution == "uncertain":
		return false, false
	case result.HTTPStatus != 0:
		return false, false
	case strings.HasPrefix(result.Detail, NodeProbeFailed), strings.HasPrefix(result.Detail, UncertainFailure):
		return false, false
	default: // 经节点发请求超时/连不上：节点失败证据
		return false, true
	}
}

// commitBusinessWave 一波 L2 结论落账（含状态机推进与观测行）。
func (h *HealthChecker) commitBusinessWave(ctx context.Context, probed []struct {
	row    nodeRow
	result BusinessResult
}, now time.Time, targetURL string) ([]BusinessOutcome, error) {
	if len(probed) == 0 {
		return nil, nil
	}
	var outcomes []BusinessOutcome
	err := h.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		outcomes = nil
		if err := EnsureHealthSchema(ctx, conn); err != nil {
			return err
		}
		ids := make([]string, len(probed))
		for i, p := range probed {
			ids[i] = p.row.NodeID
		}
		fresh, err := loadNodeRowsByID(ctx, conn, ids)
		if err != nil {
			return err
		}
		for _, p := range probed {
			row, ok := fresh[p.row.NodeID]
			if !ok {
				continue
			}
			outcome := BusinessOutcome{
				NodeID: row.NodeID, RuntimeName: p.result.RuntimeName, OK: p.result.OK,
				HTTPStatus: p.result.HTTPStatus, FinalPriceCents: p.result.FinalPriceCents,
				Detail: p.result.Detail, LatencyMS: p.result.LatencyMS, Attribution: p.result.Attribution,
			}
			// 只有有效业务响应能晋升；失败结论按归因决定是否计节点失败证据。
			if probeOK, counted := businessProbeOK(p.result); counted {
				failures := row.ConsecutiveFailures
				if probeOK {
					failures = 0
				} else {
					failures++
				}
				// 池文件成员 = 来源在场；闸内新鲜行上评估，防陈旧基线覆盖
				targetState, err := EvaluateNodeState(row.State, EvaluateNodeStateInput{
					SourceSeen:          row.SubscriptionID.Valid,
					ProbeOK:             &probeOK,
					ConsecutiveFailures: int(failures),
				})
				if err != nil {
					return err
				}
				if _, err := conn.ExecContext(ctx,
					`UPDATE proxy_nodes SET consecutive_failures = ?, state = ?, last_l2_at = ? WHERE id = ?`,
					failures, targetState, pyTimeText(now), row.ID); err != nil {
					return err
				}
				outcome.State = targetState
			} else {
				outcome.State = row.State
			}
			if err := insertObservation(ctx, conn, HealthObservationRow{
				NodeID: row.NodeID, Level: ProbeLevelL2, OK: p.result.OK,
				LatencyMS: p.result.LatencyMS, Detail: p.result.Detail,
				ObservedAt: now, Channel: DefaultScheduleChannel, Target: targetURL,
			}); err != nil {
				return err
			}
			outcomes = append(outcomes, outcome)
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return outcomes, nil
}

// ProbeOutcome 一轮 L0 的账目（供调用方记录/断言）。
type ProbeOutcome struct {
	NodeID      string
	RuntimeName string
	OK          bool
	DelayMS     int64
	Detail      string
}

// L0CheckPool 对池内每个节点做一次 L0 传输探测：只落观测与延迟，不推状态。
// 节点生死判据已归 L2 业务探测；L0 成功只把延迟挂到节点行（排序键的持久化
// 形态），观测行只留失败证据。波内按 ProbeConcurrency 并发探，块完一次短提交
// ——写事务不跨网络等待。
func (h *HealthChecker) L0CheckPool(ctx context.Context, names []string, opts ProbeOptions) ([]ProbeOutcome, error) {
	now := h.now()
	byName, err := loadNodeRowsByRuntime(ctx, h.DB, names)
	if err != nil {
		return nil, err
	}
	target := opts.URL
	if target == "" {
		target = ProbeTargetURL
	}
	var outcomes []ProbeOutcome
	sem := make(chan struct{}, ProbeConcurrency)
	for start := 0; start < len(names); start += ProbeCommitEvery {
		end := start + ProbeCommitEvery
		if end > len(names) {
			end = len(names)
		}
		chunk := names[start:end]
		results := make([]ProbeResult, len(chunk))
		var wg sync.WaitGroup
		for i, name := range chunk {
			if _, ok := byName[name]; !ok {
				continue
			}
			wg.Add(1)
			go func(i int, name string) {
				sem <- struct{}{}
				defer func() { <-sem; wg.Done() }()
				results[i] = ProbeNode(ctx, h.ControllerURL, h.Secret, name, ProbeOptions{
					URL: target, TimeoutMS: opts.TimeoutMS, ClientTimeout: opts.ClientTimeout,
				})
			}(i, name)
		}
		wg.Wait()
		failed := make([]HealthObservationRow, 0, len(chunk))
		type nodeUpdate struct {
			id      int64
			delayMS int64
		}
		var delays []nodeUpdate
		for i, name := range chunk {
			row, ok := byName[name]
			if !ok {
				continue
			}
			result := results[i]
			outcomes = append(outcomes, ProbeOutcome{
				NodeID: row.NodeID, RuntimeName: result.RuntimeName, OK: result.OK,
				DelayMS: result.DelayMS, Detail: result.Detail,
			})
			// L0 不推状态：成功只挂延迟，失败只留观测证据。
			if result.OK && result.DelayMS > 0 {
				delays = append(delays, nodeUpdate{id: row.ID, delayMS: result.DelayMS})
			} else {
				failed = append(failed, HealthObservationRow{
					NodeID: row.NodeID, Level: ProbeLevelL0, OK: false,
					Detail: result.Detail, ObservedAt: now,
					Channel: DefaultScheduleChannel, Target: target,
				})
			}
		}
		if len(delays) == 0 && len(failed) == 0 {
			continue
		}
		if err := h.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
			if err := EnsureHealthSchema(ctx, conn); err != nil {
				return err
			}
			for _, u := range delays {
				if _, err := conn.ExecContext(ctx,
					`UPDATE proxy_nodes SET l0_delay_ms = ?, last_l0_at = ? WHERE id = ?`,
					u.delayMS, pyTimeText(now), u.id); err != nil {
					return err
				}
			}
			for _, obs := range failed {
				if err := insertObservation(ctx, conn, obs); err != nil {
					return err
				}
			}
			return nil
		}); err != nil {
			return outcomes, err
		}
	}
	return outcomes, nil
}

// ── 手动体检落账（bridge.ingest_node_check 对齐）───────────────────────────

// CheckRow 一次手动体检的逐节点结果。
type CheckRow struct {
	Name        string
	Alive       bool
	ExitIP      string
	MS          int64
	SteamStatus int  // 0 = 无状态（未收到任何 HTTP 响应）
	HasSteam    bool // SteamStatus 是否在场
	Probed      bool // false = 本轮没探到（跳过，不落账）
}

// IngestionResult 一次对齐的账目摘要（只记事实，不下结论）。
type IngestionResult struct {
	Matched      int
	Activated    int
	Deactivated  int
	ExitIPsAdded int
}

// Changed 是否有任何状态/出口变化（供调用方决定重建等后续动作）。
func (r IngestionResult) Changed() bool {
	return r.Activated != 0 || r.Deactivated != 0 || r.ExitIPsAdded != 0
}

// IngestNodeCheck 把一轮手动体检结果对齐进池账本（只执行 SQL 不提交——健康、
// 观测与冷却必须与调用方在同一事务；提交归调用方，失败由写闸整体回滚）。
// runID=0 表示不挂体检运行行的零散观测。
func IngestNodeCheck(ctx context.Context, conn *sql.Conn, subscriptionID int64, results []CheckRow, nodeIDs map[string]string, now time.Time, runID int64, channel string) (IngestionResult, error) {
	if channel == "" {
		channel = DefaultIngestChannel
	}
	ids := make([]string, 0, len(nodeIDs))
	seen := map[string]bool{}
	for _, id := range nodeIDs {
		if id != "" && !seen[id] {
			seen[id] = true
			ids = append(ids, id)
		}
	}
	byID, err := loadNodeRowsByID(ctx, conn, ids)
	if err != nil {
		return IngestionResult{}, err
	}
	outcome := IngestionResult{}
	for _, row := range results {
		if !row.Probed {
			continue
		}
		node, ok := byID[nodeIDs[row.Name]]
		if !ok {
			return IngestionResult{}, fmt.Errorf("运行节点身份未登记，体检结果未落账")
		}
		if node.SubscriptionID.Valid && node.SubscriptionID.Int64 != subscriptionID {
			return IngestionResult{}, fmt.Errorf("运行节点身份已归属其他订阅，体检结果未落账")
		}
		outcome.Matched++
		if !row.Alive {
			exitAdded := false
			if row.ExitIP != "" {
				exitAdded = !node.ExitIP.Valid || node.ExitIP.String == ""
				if _, err := conn.ExecContext(ctx,
					`UPDATE proxy_nodes SET exit_ip = ?, last_l1_at = ? WHERE id = ?`,
					row.ExitIP, pyTimeText(now), node.ID); err != nil {
					return outcome, err
				}
				if err := insertObservation(ctx, conn, HealthObservationRow{
					NodeID: node.NodeID, Level: ProbeLevelL1, OK: true,
					Detail: DetailExitIP, ObservedAt: now, RunID: runID, Channel: channel,
				}); err != nil {
					return outcome, err
				}
				if exitAdded {
					outcome.ExitIPsAdded++
				}
			}
			// Steam 与出口身份都拿不到 = 节点传输失败：失败计数 +1、状态机
			// fail-fast、按档位落冷却；收到任何 Steam 响应只记观测不罚节点
			// 且清冷却（失败不写墓碑，回 ACTIVE 靠恢复轮或下一轮成功证据）。
			if !row.HasSteam && row.ExitIP == "" {
				failures := node.ConsecutiveFailures + 1
				target, err := EvaluateNodeState(node.State, EvaluateNodeStateInput{
					SourceSeen:          true,
					ProbeOK:             probeBoolPtr(false),
					ConsecutiveFailures: int(failures),
				})
				if err != nil {
					return outcome, err
				}
				if _, err := conn.ExecContext(ctx,
					`UPDATE proxy_nodes SET consecutive_failures = ?, state = ?, last_l2_at = ?,
					 cooldown_until = ? WHERE id = ?`,
					failures, target, pyTimeText(now),
					pyTimeText(now.Add(time.Duration(CooldownMinutesFor(failures))*time.Minute)),
					node.ID); err != nil {
					return outcome, err
				}
				if target != node.State {
					outcome.Deactivated++
				}
			} else if _, err := conn.ExecContext(ctx,
				`UPDATE proxy_nodes SET cooldown_until = NULL WHERE id = ?`, node.ID); err != nil {
				return outcome, err
			}
			detail := DetailSteamHTTPError
			switch {
			case !row.HasSteam && row.ExitIP == "":
				detail = DetailSteamTransport
			case row.SteamStatus == 200:
				detail = DetailSteamInvalid
			case row.SteamStatus == 502:
				detail = DetailSteamHTTP502
			case row.SteamStatus == 504:
				detail = DetailSteamHTTP504
			}
			if err := insertObservation(ctx, conn, HealthObservationRow{
				NodeID: node.NodeID, Level: ProbeLevelL2, OK: false,
				LatencyMS: row.MS, Detail: detail, ObservedAt: now, RunID: runID, Channel: channel,
			}); err != nil {
				return outcome, err
			}
			continue
		}
		// 成功证据：状态机推进（复活 + 保持 ACTIVE）、失败清零、冷却复位。
		target, err := EvaluateNodeState(node.State, EvaluateNodeStateInput{
			SourceSeen: true, ProbeOK: probeBoolPtr(true), ConsecutiveFailures: 0,
		})
		if err != nil {
			return outcome, err
		}
		stateChanged := target != node.State
		if stateChanged {
			outcome.Activated++
		}
		exitAdded := false
		exitIPArg := any(nil)
		if row.ExitIP != "" {
			exitAdded = !node.ExitIP.Valid || node.ExitIP.String == ""
			exitIPArg = row.ExitIP
			if exitAdded {
				outcome.ExitIPsAdded++
			}
		}
		lastL2Arg := any(nil)
		if row.MS > 0 {
			lastL2Arg = pyTimeText(now)
		}
		if _, err := conn.ExecContext(ctx,
			`UPDATE proxy_nodes SET state = ?, consecutive_failures = 0, cooldown_until = NULL,
			 last_l2_at = COALESCE(?, last_l2_at),
			 exit_ip = COALESCE(?, exit_ip),
			 last_l1_at = CASE WHEN ? IS NOT NULL THEN ? ELSE last_l1_at END
			 WHERE id = ?`,
			target, lastL2Arg, exitIPArg, exitIPArg, exitIPArg, node.ID); err != nil {
			return outcome, err
		}
		// 带 run_id 的轮次逐节点落明细；周期账本（run_id=0）只在变化时落。
		if runID != 0 || stateChanged || exitAdded {
			if err := insertObservation(ctx, conn, HealthObservationRow{
				NodeID: node.NodeID, Level: ProbeLevelL2, OK: true,
				LatencyMS: row.MS, Detail: DetailSteamOK, ObservedAt: now, RunID: runID, Channel: channel,
			}); err != nil {
				return outcome, err
			}
			if row.ExitIP != "" {
				if err := insertObservation(ctx, conn, HealthObservationRow{
					NodeID: node.NodeID, Level: ProbeLevelL1, OK: true,
					Detail: DetailExitIP, ObservedAt: now, RunID: runID, Channel: channel,
				}); err != nil {
					return outcome, err
				}
			}
		}
	}
	return outcome, nil
}

func probeBoolPtr(b bool) *bool { return &b }

// ── 健康台账两表 DDL（逐字对齐 Python 模型 create_all 产物）─────────────────

const ddlHealthObservations = `CREATE TABLE IF NOT EXISTS health_observations (
	id INTEGER NOT NULL,
	node_id VARCHAR(64) NOT NULL,
	level VARCHAR(2) NOT NULL,
	ok BOOLEAN NOT NULL,
	latency_ms INTEGER,
	detail TEXT,
	observed_at DATETIME,
	run_id INTEGER,
	channel VARCHAR(16),
	target VARCHAR(500),
	PRIMARY KEY (id)
)`

const ddlHealthRuns = `CREATE TABLE IF NOT EXISTS health_runs (
	id INTEGER NOT NULL,
	channel VARCHAR(16) NOT NULL,
	subscription_id INTEGER,
	started_at DATETIME NOT NULL,
	finished_at DATETIME,
	duration_ms INTEGER,
	total INTEGER,
	steam_ok INTEGER,
	ip_known INTEGER,
	failed INTEGER,
	end_reason VARCHAR(30),
	PRIMARY KEY (id)
)`

var healthSchemaIndexes = []string{
	"CREATE INDEX IF NOT EXISTS ix_health_observations_node_id ON health_observations (node_id)",
	"CREATE INDEX IF NOT EXISTS ix_health_observations_run_id ON health_observations (run_id)",
	"CREATE INDEX IF NOT EXISTS ix_health_runs_channel ON health_runs (channel)",
	"CREATE INDEX IF NOT EXISTS ix_health_runs_subscription_id ON health_runs (subscription_id)",
}

// EnsureHealthSchema 幂等建健康台账两表与索引；ctx 由写闸租约的事务传入。
func EnsureHealthSchema(ctx context.Context, exec interface {
	ExecContext(ctx context.Context, query string, args ...any) (sql.Result, error)
}) error {
	for _, ddl := range []string{ddlHealthObservations, ddlHealthRuns} {
		if _, err := exec.ExecContext(ctx, ddl); err != nil {
			return err
		}
	}
	for _, ddl := range healthSchemaIndexes {
		if _, err := exec.ExecContext(ctx, ddl); err != nil {
			return err
		}
	}
	return nil
}
