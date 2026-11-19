package proxypool

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"math/rand/v2"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/clashkernel"
	"holdexar/server-go/internal/httpx"
	"holdexar/server-go/internal/netenv"
	"holdexar/server-go/internal/secretbox"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
)

var (
	localMixedPorts = []int{7897}
)

const (
	speedRefMS  = 12000
	speedFloor  = 0.5
	untestedSpd = 0.7
	weightScale = 100000
	maxFails    = 5
)

type UserProxyOptions struct {
	KernelStatus func() (running bool, port int)
}

type userProxyView struct {
	ID                  int64   `json:"id"`
	Label               *string `json:"label"`
	Scheme              string  `json:"scheme"`
	Host                string  `json:"host"`
	Port                int     `json:"port"`
	HasAuth             bool    `json:"hasAuth"`
	URL                 string  `json:"url"`
	Enabled             bool    `json:"enabled"`
	Status              string  `json:"status"`
	LatencyMS           *int64  `json:"latencyMs"`
	ConsecutiveFailures int     `json:"consecutiveFailures"`
	LastCheckedAt       *string `json:"lastCheckedAt"`
}

func RegisterUserProxyRoutes(mux *http.ServeMux, db *sql.DB, gate *store.WriteGate, dataDir string, opts UserProxyOptions) {
	h := &userProxyHTTP{db: db, gate: gate, dataDir: dataDir, kernelStatus: opts.KernelStatus}
	mux.HandleFunc("POST /api/v1/proxies", h.add)
	mux.HandleFunc("PUT /api/v1/proxies/{proxy_id}", h.update)
	mux.HandleFunc("DELETE /api/v1/proxies/{proxy_id}", h.remove)
	mux.HandleFunc("GET /api/v1/proxies/resolve", h.resolve)
}

type userProxyHTTP struct {
	db           *sql.DB
	gate         *store.WriteGate
	dataDir      string
	kernelStatus func() (running bool, port int)
}

type proxyRow struct {
	ID                  int64
	Label               sql.NullString
	Scheme, Host        string
	Port                int
	Username, Password  sql.NullString
	Enabled             bool
	Status              string
	Latency             sql.NullInt64
	ConsecutiveFailures int
	LastCheckedAt       sql.NullString
}

func (h *userProxyHTTP) loadProxy(ctx context.Context, id int64) (*proxyRow, error) {
	row := h.db.QueryRowContext(ctx, `SELECT id,label,scheme,host,port,username,password,enabled,status,
		latency_ms,consecutive_failures,CAST(last_checked_at AS TEXT)
		FROM proxies WHERE id=?`, id)
	var p proxyRow
	err := row.Scan(&p.ID, &p.Label, &p.Scheme, &p.Host, &p.Port, &p.Username, &p.Password, &p.Enabled,
		&p.Status, &p.Latency, &p.ConsecutiveFailures, &p.LastCheckedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return &p, err
}

func projectProxy(p *proxyRow) userProxyView {
	view := userProxyView{
		ID: p.ID, Scheme: p.Scheme, Host: p.Host, Port: p.Port,
		HasAuth:             p.Username.Valid && p.Username.String != "",
		URL:                 fmt.Sprintf("%s://%s:%d", p.Scheme, p.Host, p.Port),
		Enabled:             p.Enabled,
		Status:              p.Status,
		ConsecutiveFailures: p.ConsecutiveFailures,
	}
	if view.HasAuth {
		view.URL = fmt.Sprintf("%s://***@%s:%d", p.Scheme, p.Host, p.Port)
	}
	if p.Label.Valid && p.Label.String != "" {
		label := p.Label.String
		view.Label = &label
	}
	if p.Latency.Valid {
		latency := p.Latency.Int64
		view.LatencyMS = &latency
	}
	if p.LastCheckedAt.Valid && p.LastCheckedAt.String != "" {
		at := strings.Replace(p.LastCheckedAt.String, " ", "T", 1)
		view.LastCheckedAt = &at
	}
	return view
}

type userProxyErr = httpx.ValErr

type proxyAddBody struct {
	URL      *string `json:"-"`
	Scheme   *string
	Host     *string
	Port     *int64
	Username *string
	Password *string
	Label    *string
}

func decodeProxyAdd(w http.ResponseWriter, r *http.Request) (*proxyAddBody, bool) {
	fail := func(errs []userProxyErr) (*proxyAddBody, bool) {
		runResponse(w, http.StatusUnprocessableEntity, map[string]any{"detail": errs})
		return nil, false
	}
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 1<<20))
	if err != nil {
		return fail([]userProxyErr{{Type: "missing", Loc: []any{"body"}, Msg: "Field required", Input: nil}})
	}
	if strings.TrimSpace(string(raw)) == "" {
		return fail([]userProxyErr{{Type: "missing", Loc: []any{"body"}, Msg: "Field required", Input: nil}})
	}
	var top any
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.UseNumber()
	if err := dec.Decode(&top); err != nil {
		return fail([]userProxyErr{{Type: "json_invalid", Loc: []any{"body"}, Msg: "JSON decode error", Input: map[string]any{}}})
	}
	fields, ok := top.(map[string]any)
	if !ok {
		return fail([]userProxyErr{{Type: "model_attributes_type", Loc: []any{"body"},
			Msg: "Input should be a valid dictionary or object to extract fields from", Input: top}})
	}
	body := &proxyAddBody{}
	var errs []userProxyErr
	strField := func(name string, target **string) {
		value, present := fields[name]
		if !present || value == nil {
			return
		}
		text, ok := value.(string)
		if !ok {
			errs = append(errs, userProxyErr{Type: "string_type", Loc: []any{"body", name},
				Msg: "Input should be a valid string", Input: value})
			return
		}
		*target = &text
	}
	strField("url", &body.URL)
	strField("scheme", &body.Scheme)
	strField("host", &body.Host)
	strField("username", &body.Username)
	strField("password", &body.Password)
	strField("label", &body.Label)
	if value, present := fields["port"]; present && value != nil {
		n, err := laxInt(value)
		if err != nil {
			errs = append(errs, userProxyErr{Type: err.errType, Loc: []any{"body", "port"}, Msg: err.msg, Input: value})
		} else {
			body.Port = &n
		}
	}
	if len(errs) > 0 {
		return fail(errs)
	}
	return body, true
}

type laxErr struct {
	errType string
	msg     string
}

func laxInt(v any) (int64, *laxErr) {
	switch x := v.(type) {
	case bool:
		if x {
			return 1, nil
		}
		return 0, nil
	case json.Number:
		if i, err := strconv.ParseInt(x.String(), 10, 64); err == nil {
			return i, nil
		}
		if f, err := strconv.ParseFloat(x.String(), 64); err == nil && f == float64(int64(f)) {
			return int64(f), nil
		}
		return 0, &laxErr{"int_from_float", "Input should be a valid integer, got a number with a fractional part"}
	case string:
		if i, err := strconv.ParseInt(strings.TrimSpace(x), 10, 64); err == nil {
			return i, nil
		}
		return 0, &laxErr{"int_parsing", "Input should be a valid integer, unable to parse string as an integer"}
	case float64:
		if x == float64(int64(x)) {
			return int64(x), nil
		}
		return 0, &laxErr{"int_from_float", "Input should be a valid integer, got a number with a fractional part"}
	}
	return 0, &laxErr{"int_parsing", "Input should be a valid integer, unable to parse string as an integer"}
}

type parsedProxy struct {
	scheme, host string
	port         int
	username     *string
	password     *string
}

func parseProxyURL(raw string) (*parsedProxy, error) {
	original := strings.TrimSpace(raw)
	raw = original
	if !strings.Contains(raw, "://") {
		raw = "http://" + raw
	}
	parsed, perr := url.Parse(raw)
	if perr != nil {
		return nil, fmt.Errorf("无法解析代理地址: %s", original)
	}
	hostname := parsed.Hostname()
	if hostname == "" {
		return nil, fmt.Errorf("无法解析代理地址: %s", original)
	}
	portRaw := parsed.Port()
	if portRaw == "" {
		return nil, fmt.Errorf("无法解析代理地址: %s", original)
	}
	port, err := strconv.Atoi(portRaw)
	if err != nil {
		return nil, fmt.Errorf("无法解析代理地址: %s", original)
	}
	if port < 0 || port > 65535 {
		return nil, errors.New("Port out of range 0-65535")
	}
	out := &parsedProxy{scheme: strings.ToLower(parsed.Scheme), host: strings.ToLower(hostname), port: port}
	if out.scheme == "https" {
		out.scheme = "http"
	}
	if parsed.User != nil {
		user := parsed.User.Username()
		pass, hasPass := parsed.User.Password()
		if user != "" {
			out.username = &user
		}
		if hasPass {
			out.password = &pass
		}
	}
	return out, nil
}

func (h *userProxyHTTP) addProxy(ctx context.Context, line string, scheme, host string, port int64,
	username, password, label *string) (userProxyView, error) {
	if line != "" {
		parsed, uerr := parseProxyURL(line)
		if uerr != nil {
			return userProxyView{}, uerr
		}
		scheme, host, port = parsed.scheme, parsed.host, int64(parsed.port)
		username, password = parsed.username, parsed.password
	}
	if host == "" || port == 0 {
		return userProxyView{}, errors.New("host/port 不能为空")
	}
	if scheme != "http" && scheme != "socks5" {
		return userProxyView{}, fmt.Errorf("不支持的协议: %s", scheme)
	}
	sealedUser, sealedPass := "", ""
	if username != nil && *username != "" {
		sealed, err := sealProxyCredential(h.dataDir, *username)
		if err != nil {
			return userProxyView{}, err
		}
		sealedUser = sealed
	}
	if password != nil && *password != "" {
		sealed, err := sealProxyCredential(h.dataDir, *password)
		if err != nil {
			return userProxyView{}, err
		}
		sealedPass = sealed
	}
	var view userProxyView
	err := h.gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		result, err := conn.ExecContext(ctx, `INSERT INTO proxies(label,scheme,host,port,username,password,enabled,status,consecutive_failures,created_at)
			VALUES(?,?,?,?,?,?,1,'unknown',0,?)`, labelString(label), scheme, host, port,
			nullString(sealedUser), nullString(sealedPass), pyTimeText(time.Now()))
		if err != nil {
			return err
		}
		id, err := result.LastInsertId()
		if err != nil {
			return err
		}
		row, err := loadProxyRow(ctx, conn, id)
		if err != nil {
			return err
		}
		view = projectProxy(row)
		return nil
	})
	if err != nil {
		return userProxyView{}, err
	}
	return view, nil
}

func (h *userProxyHTTP) add(w http.ResponseWriter, r *http.Request) {
	body, ok := decodeProxyAdd(w, r)
	if !ok {
		return
	}
	ctx := r.Context()
	added := []userProxyView{}
	badRequest := func(err error) {
		runResponse(w, http.StatusBadRequest, map[string]any{"detail": err.Error()})
	}
	if body.URL != nil && strings.Contains(*body.URL, "\n") {
		for _, line := range strings.Split(*body.URL, "\n") {
			line = strings.TrimSpace(line)
			if line == "" {
				continue
			}
			view, err := h.addProxy(ctx, line, "http", "", 0, nil, nil, nil)
			if err != nil {
				badRequest(err)
				return
			}
			added = append(added, view)
		}
		runResponse(w, http.StatusOK, map[string]any{"added": len(added), "items": added})
		return
	}
	scheme := "http"
	if body.Scheme != nil {
		scheme = *body.Scheme
	}
	host := ""
	if body.Host != nil {
		host = *body.Host
	}
	var port int64
	if body.Port != nil {
		port = *body.Port
	}
	view, err := h.addProxy(ctx, stringOrEmpty(body.URL), scheme, host, port, body.Username, body.Password, body.Label)
	if err != nil {
		badRequest(err)
		return
	}
	runResponse(w, http.StatusOK, map[string]any{"added": 1, "items": []userProxyView{view}})
}

func (h *userProxyHTTP) update(w http.ResponseWriter, r *http.Request) {
	id, ok := routeID(w, r, "proxy_id")
	if !ok {
		return
	}
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 1<<20))
	if err != nil {
		runResponse(w, http.StatusUnprocessableEntity, map[string]any{"detail": []userProxyErr{
			{Type: "missing", Loc: []any{"body"}, Msg: "Field required"}}})
		return
	}
	trimmed := strings.TrimSpace(string(raw))
	if trimmed == "" {
		runResponse(w, http.StatusUnprocessableEntity, map[string]any{"detail": []userProxyErr{
			{Type: "missing", Loc: []any{"body"}, Msg: "Field required", Input: nil}}})
		return
	}
	var top any
	dec := json.NewDecoder(strings.NewReader(trimmed))
	dec.UseNumber()
	if err := dec.Decode(&top); err != nil {
		runResponse(w, http.StatusUnprocessableEntity, map[string]any{"detail": []userProxyErr{
			{Type: "json_invalid", Loc: []any{"body"}, Msg: "JSON decode error", Input: map[string]any{}}}})
		return
	}
	fields, ok := top.(map[string]any)
	if !ok {
		runResponse(w, http.StatusUnprocessableEntity, map[string]any{"detail": []userProxyErr{
			{Type: "model_attributes_type", Loc: []any{"body"},
				Msg: "Input should be a valid dictionary or object to extract fields from", Input: top}}})
		return
	}
	var enabled *bool
	var label *string
	var errs []userProxyErr
	if value, present := fields["enabled"]; present && value != nil {
		switch v := value.(type) {
		case bool:
			enabled = &v
		case json.Number:
			errs = append(errs, userProxyErr{Type: "bool_parsing", Loc: []any{"body", "enabled"},
				Msg: "Input should be a valid boolean, unable to interpret input", Input: value})
		default:
			errs = append(errs, userProxyErr{Type: "bool_type", Loc: []any{"body", "enabled"},
				Msg: "Input should be a valid boolean", Input: value})
		}
	}
	if value, present := fields["label"]; present && value != nil {
		text, isStr := value.(string)
		if !isStr {
			errs = append(errs, userProxyErr{Type: "string_type", Loc: []any{"body", "label"},
				Msg: "Input should be a valid string", Input: value})
		} else {
			label = &text
		}
	}
	if len(errs) > 0 {
		runResponse(w, http.StatusUnprocessableEntity, map[string]any{"detail": errs})
		return
	}
	ctx := r.Context()
	var view userProxyView
	var missing bool
	err = h.gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		row, err := loadProxyRow(ctx, conn, id)
		if err != nil {
			return err
		}
		if row == nil {
			missing = true
			return nil
		}
		if enabled != nil {
			if _, err := conn.ExecContext(ctx, `UPDATE proxies SET enabled=?,consecutive_failures=0 WHERE id=?`,
				*enabled, id); err != nil {
				return err
			}
		}
		if label != nil {
			if _, err := conn.ExecContext(ctx, `UPDATE proxies SET label=? WHERE id=?`,
				labelString(label), id); err != nil {
				return err
			}
		}
		row, err = loadProxyRow(ctx, conn, id)
		if err != nil || row == nil {
			return err
		}
		view = projectProxy(row)
		return nil
	})
	switch {
	case err != nil:
		runPayload(w, "", err)
	case missing:
		runResponse(w, http.StatusNotFound, map[string]any{"detail": "代理不存在"})
	default:
		runResponse(w, http.StatusOK, view)
	}
}

func (h *userProxyHTTP) remove(w http.ResponseWriter, r *http.Request) {
	id, ok := routeID(w, r, "proxy_id")
	if !ok {
		return
	}
	var missing bool
	err := h.gate.WithWrite(r.Context(), store.PriorityInteractive, func(conn *sql.Conn) error {
		result, err := conn.ExecContext(r.Context(), `DELETE FROM proxies WHERE id=?`, id)
		if err != nil {
			return err
		}
		if affected, err := result.RowsAffected(); err == nil && affected == 0 {
			missing = true
		}
		return nil
	})
	switch {
	case err != nil:
		runPayload(w, "", err)
	case missing:
		runResponse(w, http.StatusNotFound, map[string]any{"detail": "代理不存在"})
	default:
		runResponse(w, http.StatusOK, map[string]any{"removed": true})
	}
}

func routeID(w http.ResponseWriter, r *http.Request, name string) (int64, bool) {
	id, err := strconv.ParseInt(r.PathValue(name), 10, 64)
	if err != nil {
		runResponse(w, http.StatusUnprocessableEntity, map[string]any{"detail": []userProxyErr{
			{Type: "int_parsing", Loc: []any{"path", name},
				Msg:   "Input should be a valid integer, unable to parse string as an integer",
				Input: r.PathValue(name)}}})
		return 0, false
	}
	return id, true
}

func (h *userProxyHTTP) resolve(w http.ResponseWriter, r *http.Request) {
	proxyURL, runtimeErr, err := h.resolveProxyURL(r.Context())
	switch {
	case err != nil:
		runPayload(w, "", err)
	case runtimeErr != nil:
		runResponse(w, http.StatusOK, resolveOut{ProxyURL: nil, Message: runtimeErr})
	default:
		runResponse(w, http.StatusOK, resolveOut{ProxyURL: proxyURL})
	}
}

type resolveOut struct {
	ProxyURL *string `json:"proxyUrl"`
	Message  *string `json:"message,omitempty"`
}

type StrategyResolveOptions struct {
	KernelStatus            func() (running bool, port int)
	PortListening           func(port int) bool
	SystemProxy             func(context.Context) (string, error)
	LocalFallback           func() int
	IncludeKernelOnFallback bool
	FailSoft                bool
}

func (h *userProxyHTTP) resolveProxyURL(ctx context.Context) (*string, *string, error) {
	return h.resolveProxyURLWith(ctx, StrategyResolveOptions{KernelStatus: h.kernelStatus})
}

func (h *userProxyHTTP) resolveProxyURLWith(ctx context.Context, opts StrategyResolveOptions) (*string, *string, error) {
	strategy := h.kvString(ctx, "proxy.strategy", "direct_only")
	direct := func() (*string, error) {
		resolve := opts.SystemProxy
		if resolve == nil {
			resolve = netenv.ResolveSystemProxy
		}
		proxyURL, err := resolve(ctx)
		if err != nil || proxyURL == "" {
			return nil, nil
		}
		return &proxyURL, nil
	}
	listening := opts.PortListening
	if listening == nil {
		listening = clashkernel.PortListening
	}
	kernelURL := func() (string, bool) {
		if opts.KernelStatus == nil {
			return "", false
		}
		running, port := opts.KernelStatus()
		if running && port > 0 && listening(port) {
			return fmt.Sprintf("http://127.0.0.1:%d", port), true
		}
		return "", false
	}
	pick := func() (*string, *string, error) {
		enabled, err := h.enabledProxies(ctx)
		if err != nil {
			return nil, nil, err
		}
		if len(enabled) == 0 {
			if opts.FailSoft {
				return nil, nil, nil
			}
			message := fmt.Sprintf("策略 %s 需要至少一条启用的代理", strategy)
			return nil, &message, nil
		}
		chosen := weightedPick(ctx, enabled, h)
		return &chosen, nil, nil
	}
	switch strategy {
	case "direct_only":
		proxyURL, err := direct()
		return proxyURL, nil, err
	case "direct_first":
		return nil, nil, nil
	case "proxy_first":
		if url, ok := kernelURL(); ok {
			return &url, nil, nil
		}
		enabled, err := h.enabledProxies(ctx)
		if err != nil {
			return nil, nil, err
		}
		if len(enabled) > 0 {
			chosen := weightedPick(ctx, enabled, h)
			return &chosen, nil, nil
		}
		fallbackFn := opts.LocalFallback
		if fallbackFn == nil {
			fallbackFn = localFallbackPort
		}
		if fallback := fallbackFn(); fallback > 0 {
			url := fmt.Sprintf("http://127.0.0.1:%d", fallback)
			return &url, nil, nil
		}
		return nil, nil, nil
	default:
		if opts.IncludeKernelOnFallback {
			if url, ok := kernelURL(); ok {
				return &url, nil, nil
			}
		}
		return pick()
	}
}

// ResolveStrategyProxy 与解析端点共用策略链；失败或拿不到代理返回空串供直连。
func ResolveStrategyProxy(ctx context.Context, db *sql.DB, gate *store.WriteGate, dataDir string, kernelStatus func() (running bool, port int)) (string, string, error) {
	return ResolveStrategyProxyWith(ctx, db, gate, dataDir, StrategyResolveOptions{KernelStatus: kernelStatus})
}

func ResolveStrategyProxyWith(ctx context.Context, db *sql.DB, gate *store.WriteGate, dataDir string, opts StrategyResolveOptions) (string, string, error) {
	h := &userProxyHTTP{db: db, gate: gate, dataDir: dataDir, kernelStatus: opts.KernelStatus}
	proxyURL, message, err := h.resolveProxyURLWith(ctx, opts)
	if proxyURL == nil {
		return "", derefString(message), err
	}
	return *proxyURL, derefString(message), err
}

func derefString(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func (h *userProxyHTTP) kvString(ctx context.Context, key, fallback string) string {
	var raw sql.NullString
	if err := h.db.QueryRowContext(ctx,
		`SELECT value_json FROM app_settings WHERE key=?`, key).Scan(&raw); err != nil || !raw.Valid {
		return fallback
	}
	var value any
	if json.Unmarshal([]byte(raw.String), &value) != nil {
		return fallback
	}
	if text, ok := value.(string); ok && text != "" {
		return text
	}
	return fallback
}

func (h *userProxyHTTP) kvInt(ctx context.Context, key string, fallback int64) int64 {
	var raw sql.NullString
	if err := h.db.QueryRowContext(ctx,
		`SELECT value_json FROM app_settings WHERE key=?`, key).Scan(&raw); err != nil || !raw.Valid {
		return fallback
	}
	var value any
	if json.Unmarshal([]byte(raw.String), &value) != nil {
		return fallback
	}
	switch x := value.(type) {
	case float64:
		return int64(x)
	case json.Number:
		if n, err := x.Int64(); err == nil {
			return n
		}
	}
	return fallback
}

func (h *userProxyHTTP) enabledProxies(ctx context.Context) ([]proxyRow, error) {
	rows, err := h.db.QueryContext(ctx, `SELECT id,label,scheme,host,port,username,password,enabled,status,
		latency_ms,consecutive_failures,CAST(last_checked_at AS TEXT) FROM proxies WHERE enabled=1 ORDER BY id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []proxyRow{}
	for rows.Next() {
		var p proxyRow
		if err := rows.Scan(&p.ID, &p.Label, &p.Scheme, &p.Host, &p.Port, &p.Username, &p.Password, &p.Enabled,
			&p.Status, &p.Latency, &p.ConsecutiveFailures, &p.LastCheckedAt); err != nil {
			return nil, err
		}
		out = append(out, p)
	}
	return out, rows.Err()
}

func speedScore(latency sql.NullInt64) float64 {
	if !latency.Valid || latency.Int64 == 0 {
		return untestedSpd
	}
	slow := math.Min(float64(latency.Int64), speedRefMS) / speedRefMS
	return speedFloor + (1.0-speedFloor)*(1.0-slow)
}

func healthScore(fails int, status string) float64 {
	if fails >= maxFails {
		return 0
	}
	if fails == 0 && status != "failed" {
		return 1.0
	}
	return math.Pow(2.0, -float64(max(fails, 1)))
}

func weightedPick(ctx context.Context, enabled []proxyRow, h *userProxyHTTP) string {
	scores := make([]float64, len(enabled))
	total := 0.0
	for i, p := range enabled {
		scores[i] = speedScore(p.Latency) * healthScore(p.ConsecutiveFailures, p.Status)
		total += scores[i]
	}
	if total <= 0 {
		return h.roundRobin(ctx, enabled)
	}
	weights := make([]int64, len(enabled))
	sum := int64(0)
	for i, score := range scores {
		weights[i] = int64(math.Round(score * weightScale))
		sum += weights[i]
	}
	target := rand.Int64N(sum)
	for i, weight := range weights {
		if target < weight {
			return runtimeURL(h.dataDir, enabled[i])
		}
		target -= weight
	}
	return runtimeURL(h.dataDir, enabled[len(enabled)-1])
}

func (h *userProxyHTTP) roundRobin(ctx context.Context, enabled []proxyRow) string {
	idx := h.kvInt(ctx, "proxy.rr_index", 0)
	chosen := enabled[idx%int64(len(enabled))]
	next := idx + 1
	_ = h.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		return settings.WriteValue(ctx, conn, "proxy.rr_index", next)
	})
	return runtimeURL(h.dataDir, chosen)
}

func quoteUserinfo(raw string) string {
	return strings.ReplaceAll(url.QueryEscape(raw), "+", "%20")
}

// runtimeURL 构造出网代理 URL；凭据解密失败退化为无认证 URL。
func runtimeURL(dataDir string, p proxyRow) string {
	if !p.Username.Valid || p.Username.String == "" {
		return fmt.Sprintf("%s://%s:%d", p.Scheme, p.Host, p.Port)
	}
	user := openProxyCredential(dataDir, p.Username.String)
	secret := openProxyCredential(dataDir, nullOrEmpty(p.Password))
	if user == "" {
		return fmt.Sprintf("%s://%s:%d", p.Scheme, p.Host, p.Port)
	}
	auth := quoteUserinfo(user) + ":" + quoteUserinfo(secret) + "@"
	return fmt.Sprintf("%s://%s%s:%d", p.Scheme, auth, p.Host, p.Port)
}

func localFallbackPort() int {
	for _, port := range localMixedPorts {
		conn, err := net.DialTimeout("tcp", fmt.Sprintf("127.0.0.1:%d", port), time.Second)
		if err != nil {
			continue
		}
		_ = conn.Close()
		if !localEgressOK(fmt.Sprintf("http://127.0.0.1:%d", port)) {
			continue
		}
		return port
	}
	return 0
}

func localEgressOK(proxyURL string) bool {
	parsed, err := url.Parse(proxyURL)
	if err != nil {
		return false
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = http.ProxyURL(parsed)
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: 8 * time.Second}
	request, err := http.NewRequest(http.MethodGet, "https://www.gstatic.com/generate_204", nil)
	if err != nil {
		return false
	}
	resp, err := client.Do(request)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode < 400
}

func sealProxyCredential(dataDir, plain string) (string, error) {
	if secretbox.IsEncrypted(plain) {
		return plain, nil
	}
	return secretbox.EncryptSecretIn(dataDir, plain, secretbox.PurposeProxyCredential)
}

func openProxyCredential(dataDir, value string) string {
	if !secretbox.IsEncrypted(value) {
		return value
	}
	plain, err := secretbox.DecryptSecretIn(dataDir, value, secretbox.PurposeProxyCredential)
	if err != nil {
		return ""
	}
	return plain
}

func loadProxyRow(ctx context.Context, conn *sql.Conn, id int64) (*proxyRow, error) {
	row := conn.QueryRowContext(ctx, `SELECT id,label,scheme,host,port,username,password,enabled,status,
		latency_ms,consecutive_failures,CAST(last_checked_at AS TEXT) FROM proxies WHERE id=?`, id)
	var p proxyRow
	err := row.Scan(&p.ID, &p.Label, &p.Scheme, &p.Host, &p.Port, &p.Username, &p.Password, &p.Enabled,
		&p.Status, &p.Latency, &p.ConsecutiveFailures, &p.LastCheckedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &p, nil
}

func labelString(label *string) any {
	if label == nil || *label == "" {
		return nil
	}
	return *label
}

func nullString(value string) any {
	if value == "" {
		return nil
	}
	return value
}

func nullOrEmpty(v sql.NullString) string {
	if !v.Valid {
		return ""
	}
	return v.String
}

func stringOrEmpty(v *string) string {
	if v == nil {
		return ""
	}
	return *v
}
