package proxypool

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/netip"
	"net/url"
	"os"
	"slices"
	"strings"
	"sync"
	"time"

	"holdexar/server-go/internal/clashcfg"
	"holdexar/server-go/internal/store"
)

func readFileText(path string) (string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	return string(raw), nil
}

func parseHTTPProxyURL(port int) (*url.URL, error) {
	return url.Parse(fmt.Sprintf("http://127.0.0.1:%d", port))
}

const (
	ProbeLevelL1 = "L1"
	ProbeLevelL2 = "L2"

	ExitIPTargetURL   = "https://api.ipify.org?format=json"
	DefaultL1Timeout  = 15 * time.Second
	DefaultL2Timeout  = 30 * time.Second
	ProbeCommitEvery  = 10
	ProbeConcurrency  = 20
	TunnelFailureStat = 502

	NodeProbeFailed    = "NODE_PROBE_FAILED"
	NodeTunnelFailed   = "NODE_TUNNEL_FAILED"
	TargetServiceFail  = "TARGET_SERVICE_FAILED"
	UncertainFailure   = "UNCERTAIN_FAILURE"
	InvalidIPResponse  = "INVALID_IP_RESPONSE"
	BusinessOK         = "BUSINESS_OK"
	InvalidBizResponse = "INVALID_BUSINESS_RESPONSE"

	DefaultBusinessAppID = 220
	BusinessCC           = "us"

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

const browseProbeURL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"

const storeBrowseSafeChars = `{}",:~-_.`

func BusinessProbeURL(appID int64, cc string) string {
	if cc == "" {
		cc = BusinessCC
	}
	body := fmt.Sprintf(`{"ids":[{"appid":%d}],"context":{"language":"english",`+
		`"country_code":"%s","steam_realm":1},"data_request":`+
		`{"include_basic_info":true,"include_all_purchase_options":true}}`, appID, strings.ToUpper(cc))
	return browseProbeURL + "?input_json=" + storeBrowseQuote(body)
}

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

type ExitIPResult struct {
	RuntimeName string
	OK          bool
	ExitIP      string
	Detail      string
	LatencyMS   int64
}

type BusinessResult struct {
	RuntimeName     string
	OK              bool
	HTTPStatus      int
	FinalPriceCents int64
	Detail          string
	LatencyMS       int64
	Attribution     string
}

func selectSelector(ctx context.Context, controllerURL, secret, selector, runtimeName string, timeout time.Duration) string {
	client := probeHTTPClient(timeout)
	defer client.CloseIdleConnections()
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
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
	if status < 200 || status >= 300 {
		return fmt.Sprintf("%s: 选择节点失败（HTTP %d）", NodeProbeFailed, status)
	}
	status, raw, err := doJSON(http.MethodGet, "/proxies/"+pyURLPathEscape(selector), "")
	if err != nil {
		return fmt.Sprintf("%s: 控制器不可达（%T）", NodeProbeFailed, err)
	}
	if status != http.StatusOK {
		return fmt.Sprintf("%s: 回读节点失败（HTTP %d）", NodeProbeFailed, status)
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

func pyURLPathEscape(s string) string {
	return url.PathEscape(s)
}

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

type ExitProbeOptions struct {
	Selector string
	URL      string
	Timeout  time.Duration
	RunID    int64
}

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

type BusinessProbeOptions struct {
	Selector string
	URL      string
	AppID    int64
	Timeout  time.Duration
	RunID    int64
}

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

func (h *HealthChecker) Lanes() []clashcfg.ProbeLane {
	raw, err := readFileText(h.ConfigPath)
	if err != nil || raw == "" {
		return nil
	}
	return PoolProbeLanes(raw)
}

func (h *HealthChecker) MixedPort() int {
	raw, err := readFileText(h.ConfigPath)
	if err != nil || raw == "" {
		return 0
	}
	return clashcfg.ParseMixedPort(raw)
}

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
	slices.Sort(uniq)
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
	slices.Sort(ids)
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

type ExitIPOutcome struct {
	NodeID      string
	RuntimeName string
	OK          bool
	ExitIP      string
	Detail      string
	LatencyMS   int64
}

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
				wg.Go(func() {
					results[i] = waveResult{name, ProbeExitIP(ctx, h.ControllerURL, h.Secret,
						lane.Port, name, ExitProbeOptions{Selector: lane.Group, URL: opts.URL, Timeout: opts.Timeout})}
				})
			}
			wg.Wait()
		} else {
			for i, name := range chunk {
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
		if err := h.commitExitIPWave(ctx, probed, now, target, opts.RunID); err != nil {
			return outcomes, err
		}
	}
	return outcomes, nil
}

func (h *HealthChecker) commitExitIPWave(ctx context.Context, probed []struct {
	row    nodeRow
	result ExitIPResult
}, now time.Time, target string, runID int64) error {
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
				ObservedAt: now, RunID: runID, Channel: DefaultScheduleChannel, Target: target,
			}); err != nil {
				return err
			}
		}
		return nil
	})
}

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
				wg.Go(func() {
					results[i] = waveResult{name, ProbeBusiness(ctx, h.ControllerURL, h.Secret,
						lane.Port, name, BusinessProbeOptions{Selector: lane.Group, URL: opts.URL,
							AppID: opts.AppID, Timeout: opts.Timeout})}
				})
			}
			wg.Wait()
		} else {
			for i, name := range chunk {
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
		waveOutcomes, err := h.commitBusinessWave(ctx, probed, now, target, opts.RunID)
		if err != nil {
			return outcomes, err
		}
		outcomes = append(outcomes, waveOutcomes...)
	}
	return outcomes, nil
}

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
	default:
		return false, true
	}
}

func (h *HealthChecker) commitBusinessWave(ctx context.Context, probed []struct {
	row    nodeRow
	result BusinessResult
}, now time.Time, targetURL string, runID int64) ([]BusinessOutcome, error) {
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
			if probeOK, counted := businessProbeOK(p.result); counted {
				failures := row.ConsecutiveFailures
				if probeOK {
					failures = 0
				} else {
					failures++
				}
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
				ObservedAt: now, RunID: runID, Channel: DefaultScheduleChannel, Target: targetURL,
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

type ProbeOutcome struct {
	NodeID      string
	RuntimeName string
	OK          bool
	DelayMS     int64
	Detail      string
}

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

type CheckRow struct {
	Name        string
	Alive       bool
	ExitIP      string
	MS          int64
	SteamStatus int
	HasSteam    bool
	Probed      bool
}

type IngestionResult struct {
	Matched      int
	Activated    int
	Deactivated  int
	ExitIPsAdded int
}

func (r IngestionResult) Changed() bool {
	return r.Activated != 0 || r.Deactivated != 0 || r.ExitIPsAdded != 0
}

// IngestNodeCheck 只执行 SQL 不提交，由调用方与写闸同一事务提交或回滚。
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
			target, lastL2Arg, exitIPArg, exitIPArg, pyTimeText(now), node.ID); err != nil {
			return outcome, err
		}
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
	total INTEGER NOT NULL,
	steam_ok INTEGER NOT NULL,
	ip_known INTEGER NOT NULL,
	failed INTEGER NOT NULL,
	end_reason VARCHAR(30),
	PRIMARY KEY (id)
)`

var healthSchemaIndexes = []string{
	"CREATE INDEX IF NOT EXISTS ix_health_observations_node_id ON health_observations (node_id)",
	"CREATE INDEX IF NOT EXISTS ix_health_observations_run_id ON health_observations (run_id)",
	"CREATE INDEX IF NOT EXISTS ix_health_runs_channel ON health_runs (channel)",
	"CREATE INDEX IF NOT EXISTS ix_health_runs_subscription_id ON health_runs (subscription_id)",
}

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
