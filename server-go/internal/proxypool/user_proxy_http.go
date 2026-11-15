// user_proxy_http.go 用户代理条目 CRUD 与策略出口解析（等价 Python
// proxies/router.py 的 POST /proxies、PUT/DELETE /{proxy_id}、GET /resolve
// 与 service.py 的 add_proxy / _parse_proxy_url / resolve_proxy_url）。
package proxypool

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"math/rand"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/clashkernel"
	"holdexar/server-go/internal/netenv"
	"holdexar/server-go/internal/secretbox"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
)

// 策略引擎加权随机的速度/健康分常量（Python service.py:88-94 同值）。
var (
	localMixedPorts = []int{7897}
)

const (
	speedRefMS  = 12000 // TEST_TIMEOUT=12s
	speedFloor  = 0.5
	untestedSpd = 0.7
	weightScale = 100000
	maxFails    = 5
)

// UserProxyOptions 用户代理路由装配选项：KernelStatus 由 serverd 注入订阅
// 内核混合口快照（proxypool 不持有内核进程）。
type UserProxyOptions struct {
	KernelStatus func() (running bool, port int)
}

// userProxyView 对外投影（Python _proxy_dict 同形；凭据一律掩码）。
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

// RegisterUserProxyRoutes 装配用户代理四端点。
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

// proxyRow proxies 行读取（凭据保持库内密封形态）。
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

// project _proxy_dict 投影（url 掩码：***@host:port）。
func projectProxy(p *proxyRow) userProxyView {
	view := userProxyView{
		ID: p.ID, Scheme: p.Scheme, Host: p.Host, Port: p.Port,
		HasAuth:              p.Username.Valid && p.Username.String != "",
		URL:                  fmt.Sprintf("%s://%s:%d", p.Scheme, p.Host, p.Port),
		Enabled:              p.Enabled,
		Status:               p.Status,
		ConsecutiveFailures:  p.ConsecutiveFailures,
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

// userProxyErr body 校验错误项（Pydantic 实测锚点形状）。
type userProxyErr struct {
	Type  string         `json:"type"`
	Loc   []any          `json:"loc"`
	Msg   string         `json:"msg"`
	Input any            `json:"input"`
	Ctx   map[string]any `json:"ctx,omitempty"`
}

type proxyAddBody struct {
	URL      *string `json:"-"`
	Scheme   *string
	Host     *string
	Port     *int64
	Username *string
	Password *string
	Label    *string
}

// decodeProxyAdd 请求体校验：缺失/非对象/字段类型错误按 Pydantic 形状回 422。
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

// laxInt Pydantic int 宽松强转（bool→0/1；整值浮点与可解析整串收下）。
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

// parsedProxy _parse_proxy_url 输出。
type parsedProxy struct {
	scheme, host string
	port         int
	username     *string
	password     *string
}

// parseProxyURL _parse_proxy_url 同构：补 scheme、https 归一 http、
// hostname 小写、端口必需。失败返回与 Python 逐字一致的 ValueError 文案。
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

// addProxy 单条入库：凭据密封 + INTERACTIVE 写（Python add_proxy 同构）。
func (h *userProxyHTTP) addProxy(ctx context.Context, line string, scheme, host string, port int64,
	username, password, label *string) (userProxyView, error) {
	if line != "" {
		parsed, uerr := parseProxyURL(line)
		if uerr != nil {
			return userProxyView{}, uerr
		}
		// URL 模式完全以解析结果为准（Python: scheme..password = parsed 整体覆盖）
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
			// 批量行不带 label（Python add_proxy(line) 同径）
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

// routeID 路径参数整数校验（Pydantic int_parsing 实测形状）。
func routeID(w http.ResponseWriter, r *http.Request, name string) (int64, bool) {
	id, err := strconv.ParseInt(r.PathValue(name), 10, 64)
	if err != nil {
		runResponse(w, http.StatusUnprocessableEntity, map[string]any{"detail": []userProxyErr{
			{Type: "int_parsing", Loc: []any{"path", name},
				Msg: "Input should be a valid integer, unable to parse string as an integer",
				Input: r.PathValue(name)}}})
		return 0, false
	}
	return id, true
}

// resolve GET /proxies/resolve：策略引擎补全（Python resolve_proxy_url 同构）。
// RuntimeError 语义映射 200 {"proxyUrl":null,"message":...}。
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

// resolveOut 策略解析响应（Python dict 键序同形）。
type resolveOut struct {
	ProxyURL *string `json:"proxyUrl"`
	Message  *string `json:"message,omitempty"`
}

// resolveProxyURL 策略引擎：direct_only=系统代理；direct_first=直连；
// proxy_first=内核端口 → 启用池节点加权随机 → 7897 兜底 → 直连；
// 其余策略（proxy_only/未知）= 启用池节点加权随机，无节点即 RuntimeError。
func (h *userProxyHTTP) resolveProxyURL(ctx context.Context) (*string, *string, error) {
	strategy := h.kvString(ctx, "proxy.strategy", "direct_only")
	direct := func() (*string, error) {
		proxyURL, err := netenv.ResolveSystemProxy(ctx)
		if err != nil || proxyURL == "" {
			return nil, nil
		}
		return &proxyURL, nil
	}
	pick := func() (*string, *string, error) {
		enabled, err := h.enabledProxies(ctx)
		if err != nil {
			return nil, nil, err
		}
		if len(enabled) == 0 {
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
		if h.kernelStatus != nil {
			if running, port := h.kernelStatus(); running && port > 0 && clashkernel.PortListening(port) {
				url := fmt.Sprintf("http://127.0.0.1:%d", port)
				return &url, nil, nil
			}
		}
		enabled, err := h.enabledProxies(ctx)
		if err != nil {
			return nil, nil, err
		}
		if len(enabled) > 0 {
			chosen := weightedPick(ctx, enabled, h)
			return &chosen, nil, nil
		}
		if fallback := localFallbackPort(); fallback > 0 {
			url := fmt.Sprintf("http://127.0.0.1:%d", fallback)
			return &url, nil, nil
		}
		return nil, nil, nil
	default:
		return pick()
	}
}

// ResolveStrategyProxy 策略引擎的程序化出站口（GET /proxies/resolve 同链）：
// 账号/愿望单/成就等 Steam 出站装配与本端点共用一条解析链。返回 (proxyURL,
// message, err)：proxyURL 为空即直连；message 非空为可读运行时说明；err 仅
// 基础设施故障。调用方拿不到代理按直连处理（strategy_proxy_or_direct 语义）。
func ResolveStrategyProxy(ctx context.Context, db *sql.DB, gate *store.WriteGate, dataDir string, kernelStatus func() (running bool, port int)) (string, string, error) {
	h := &userProxyHTTP{db: db, gate: gate, dataDir: dataDir, kernelStatus: kernelStatus}
	proxyURL, message, err := h.resolveProxyURL(ctx)
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

// enabledProxies 启用池节点（Python list_proxies(enabled_only=True) 同径）。
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

// speedScore 速度分（Python _speed 同构）。
func speedScore(latency sql.NullInt64) float64 {
	if !latency.Valid || latency.Int64 == 0 {
		return untestedSpd
	}
	slow := math.Min(float64(latency.Int64), speedRefMS) / speedRefMS
	return speedFloor + (1.0-speedFloor)*(1.0-slow)
}

// healthScore 健康分（Python _health 同构）。
func healthScore(fails int, status string) float64 {
	if fails >= maxFails {
		return 0
	}
	if fails == 0 && status != "failed" {
		return 1.0
	}
	return math.Pow(2.0, -float64(max(fails, 1)))
}

// weightedPick 加权随机（Python _weighted_pick 同构）；权重全零退回顺序轮询。
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
	target := rand.Int63n(sum)
	for i, weight := range weights {
		if target < weight {
			return runtimeURL(h.dataDir, enabled[i])
		}
		target -= weight
	}
	return runtimeURL(h.dataDir, enabled[len(enabled)-1])
}

// roundRobin 顺序轮询兜底（rr_index 存 app_settings，Python _round_robin 同径）。
func (h *userProxyHTTP) roundRobin(ctx context.Context, enabled []proxyRow) string {
	idx := h.kvInt(ctx, "proxy.rr_index", 0)
	chosen := enabled[idx%int64(len(enabled))]
	next := idx + 1
	_ = h.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		return settings.WriteValue(ctx, conn, "proxy.rr_index", next)
	})
	return runtimeURL(h.dataDir, chosen)
}

// runtimeURL 库内行 → 出网 URL（唯一解密点；密钥不可用退化无认证 URL）。
func runtimeURL(dataDir string, p proxyRow) string {
	if !p.Username.Valid || p.Username.String == "" {
		return fmt.Sprintf("%s://%s:%d", p.Scheme, p.Host, p.Port)
	}
	user := openProxyCredential(dataDir, p.Username.String)
	secret := openProxyCredential(dataDir, nullOrEmpty(p.Password))
	if user == "" {
		return fmt.Sprintf("%s://%s:%d", p.Scheme, p.Host, p.Port)
	}
	auth := url.QueryEscape(user) + ":" + url.QueryEscape(secret) + "@"
	return fmt.Sprintf("%s://%s%s:%d", p.Scheme, auth, p.Host, p.Port)
}

// localFallbackPort 外部本地兜底口：端口在听 + 经它真能出网（Python
// _local_fallback_port 同构：_tcp_alive + local_channel_egress_ok）。
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

// localEgressOK 经借道口发一次 204 探测（Python local_channel_egress_ok 同构）。
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

// ─── 小件 ───────────────────────────────────────────────────────────────────

// sealProxyCredential 凭据密封（purpose 与 rekey 解封对拍 proxy-credential）。
func sealProxyCredential(dataDir, plain string) (string, error) {
	if secretbox.IsEncrypted(plain) {
		return plain, nil
	}
	return secretbox.EncryptSecretIn(dataDir, plain, secretbox.PurposeProxyCredential)
}

// openProxyCredential 凭据解密；任何失败按不可用降级（Python open_value 同径）。
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
