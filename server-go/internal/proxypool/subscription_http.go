package proxypool

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
)

type SubscriptionView struct {
	ID               int64              `json:"id"`
	Kind             string             `json:"kind"`
	URL              string             `json:"url"`
	Label            *string            `json:"label"`
	CreatedAt        *string            `json:"createdAt"`
	LastImportedAt   *string            `json:"lastImportedAt"`
	LastStats        *subscriptionStats `json:"lastStats"`
	Deprecated       bool               `json:"deprecated"`
	DeprecatedAt     *string            `json:"deprecatedAt"`
	DeprecatedReason *string            `json:"deprecatedReason"`
	AutoRefresh      bool               `json:"autoRefresh"`
}

type subscriptionStats struct {
	Fetched     *int    `json:"fetched,omitempty"`
	Added       *int    `json:"added,omitempty"`
	Skipped     *int    `json:"skipped,omitempty"`
	Total       *int    `json:"total,omitempty"`
	Alive       *int    `json:"alive,omitempty"`
	Traffic     *string `json:"traffic,omitempty"`
	Nodes       *int    `json:"nodes,omitempty"`
	Cached      *bool   `json:"cached,omitempty"`
	LabelSource string  `json:"labelSource,omitempty"`
}

const subscriptionViewSQL = `SELECT id,kind,url,label,replace(created_at,' ','T'),replace(last_imported_at,' ','T'),
	CAST(last_stats AS TEXT),COALESCE(deprecated,0),replace(deprecated_at,' ','T'),deprecated_reason,COALESCE(auto_refresh,1)
	FROM proxy_subscriptions`

var (
	subscriptionURLInText = regexp.MustCompile(`(?i)https?://[^\s<>"']+`)
	errSubscriptionKind   = errors.New("当前仅支持管理 Clash 订阅")
)

func parseSubscriptionURL(raw string) *url.URL {
	parsed, err := url.Parse(raw)
	if err != nil || parsed.Hostname() == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") {
		return nil
	}
	return parsed
}

func maskSubscriptionURL(raw string) string {
	if raw == "" {
		return ""
	}
	if parsed := parseSubscriptionURL(raw); parsed != nil {
		return parsed.Scheme + "://" + parsed.Host + "/***"
	}
	return "***"
}

func safeSubscriptionText(raw string) string {
	return subscriptionURLInText.ReplaceAllStringFunc(raw, maskSubscriptionURL)
}

func scanSubscriptionView(row interface{ Scan(...any) error }, dataDir string, now time.Time) (SubscriptionView, error) {
	var view SubscriptionView
	var storedURL, stats string
	var rawStats sql.NullString
	if err := row.Scan(&view.ID, &view.Kind, &storedURL, &view.Label, &view.CreatedAt, &view.LastImportedAt,
		&rawStats, &view.Deprecated, &view.DeprecatedAt, &view.DeprecatedReason, &view.AutoRefresh); err != nil {
		return view, err
	}
	view.URL = maskSubscriptionURL(OpenURL(dataDir, storedURL))
	if view.Label != nil {
		*view.Label = safeSubscriptionText(*view.Label)
	}
	if view.DeprecatedReason != nil {
		*view.DeprecatedReason = safeSubscriptionText(*view.DeprecatedReason)
	}
	stats = rawStats.String
	if json.Unmarshal([]byte(stats), &view.LastStats) == nil && view.LastStats != nil {
		if view.LastStats.Traffic != nil {
			*view.LastStats.Traffic = safeSubscriptionText(*view.LastStats.Traffic)
		}
		if source := view.LastStats.LabelSource; source != "panel" && source != "user" {
			view.LastStats.LabelSource = ""
		}
	} else {
		view.LastStats = nil
	}
	if reason := SubscriptionBlockReason(stats, now); reason != "" {
		view.Deprecated, view.DeprecatedReason = true, &reason
	}
	return view, nil
}

func ListSubscriptionViews(ctx context.Context, db *sql.DB, dataDir, kind string) ([]SubscriptionView, error) {
	rows, err := db.QueryContext(ctx, subscriptionViewSQL+` WHERE (?='' OR kind=?) ORDER BY id`, kind, kind)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := []SubscriptionView{}
	now := time.Now()
	for rows.Next() {
		view, err := scanSubscriptionView(rows, dataDir, now)
		if err != nil {
			return nil, err
		}
		items = append(items, view)
	}
	return items, rows.Err()
}

// SubscriptionRouteOptions 订阅管理路由装配选项。BeforeAdd = 保存前置钩子
// （kind=clash 内核缺失自动安装，失败拒保存，错误文案进 400）；AfterAdd =
// 保存成功后的即时生效链（fail-soft，返回追加进响应的额外键，如 warning /
// kernelStarted）。两者 nil = 跳过。AfterDelete / RevertToDirect = 删除订阅
// 的收尾链（clashservice 缓存产物清理 / 无出口回退直连）；Channels 与
// KernelProxy = 明文导入的借道通道链与订阅内核混合口（均 serverd 注入）。
type SubscriptionRouteOptions struct {
	Refresh        func(context.Context, int64) (*SyncResult, error)
	OnChanged      func()
	BeforeAdd      func(ctx context.Context, kind string) error
	AfterAdd       func(ctx context.Context, subID int64) map[string]any
	AfterDelete    func(ctx context.Context, subID int64, kind string)
	RevertToDirect func(ctx context.Context)
	Channels       func(ctx context.Context) []Channel
	KernelProxy    func(ctx context.Context) string
}

type subscriptionHTTP struct {
	db             *sql.DB
	gate           *store.WriteGate
	dataDir        string
	refresh        func(context.Context, int64) (*SyncResult, error)
	onChanged      func()
	beforeAdd      func(ctx context.Context, kind string) error
	afterAdd       func(ctx context.Context, subID int64) map[string]any
	afterDelete    func(ctx context.Context, subID int64, kind string)
	revertToDirect func(ctx context.Context)
	channels       func(ctx context.Context) []Channel
	kernelProxy    func(ctx context.Context) string
}

type subscriptionEdit struct {
	Kind        string  `json:"kind"`
	URL         *string `json:"url"`
	Label       *string `json:"label"`
	AutoRefresh *bool   `json:"autoRefresh"`
}

func RegisterSubscriptionRoutes(mux *http.ServeMux, db *sql.DB, gate *store.WriteGate, dataDir string,
	opts SubscriptionRouteOptions) {
	h := subscriptionHTTP{db: db, gate: gate, dataDir: dataDir, refresh: opts.Refresh, onChanged: opts.OnChanged,
		beforeAdd: opts.BeforeAdd, afterAdd: opts.AfterAdd, afterDelete: opts.AfterDelete,
		revertToDirect: opts.RevertToDirect, channels: opts.Channels, kernelProxy: opts.KernelProxy}
	const path = "/api/v1/proxies/subscriptions"
	mux.HandleFunc("GET "+path, h.list)
	mux.HandleFunc("GET "+path+"/{id}/url", h.reveal)
	mux.HandleFunc("POST "+path, h.add)
	mux.HandleFunc("PUT "+path+"/{id}", h.update)
	mux.HandleFunc("DELETE "+path+"/{id}", h.deleteRoute)
	mux.HandleFunc("POST "+path+"/{id}/refresh", h.refreshRoute(true))
	mux.HandleFunc("POST "+path+"/{id}/sync", h.refreshRoute(false))
	mux.HandleFunc("POST "+path+"/{id}/import", h.importRoute)
}

func subscriptionError(w http.ResponseWriter, status int, detail string) {
	runResponse(w, status, map[string]string{"detail": detail})
}

func subscriptionRouteID(w http.ResponseWriter, r *http.Request) (int64, bool) {
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	if err != nil {
		subscriptionError(w, http.StatusUnprocessableEntity, "订阅编号不正确")
	}
	return id, err == nil
}

func readSubscriptionEdit(w http.ResponseWriter, r *http.Request, requireURL bool) (subscriptionEdit, bool) {
	var req *subscriptionEdit
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<10))
	if decoder.Decode(&req) != nil || req == nil || decoder.Decode(new(any)) != io.EOF {
		subscriptionError(w, http.StatusUnprocessableEntity, "订阅参数不正确")
		return subscriptionEdit{}, false
	}
	if req.URL != nil {
		*req.URL = strings.TrimSpace(*req.URL)
		parsed := parseSubscriptionURL(*req.URL)
		if parsed == nil || parsed.Path == "/***" {
			subscriptionError(w, http.StatusBadRequest, "请输入完整的 http(s) 订阅链接")
			return subscriptionEdit{}, false
		}
	} else if requireURL {
		subscriptionError(w, http.StatusUnprocessableEntity, "缺少订阅链接")
		return subscriptionEdit{}, false
	}
	if req.Label != nil {
		*req.Label = truncateRunes(strings.TrimSpace(*req.Label), 100)
	}
	return *req, true
}

func (h subscriptionHTTP) list(w http.ResponseWriter, r *http.Request) {
	items, err := ListSubscriptionViews(r.Context(), h.db, h.dataDir, r.URL.Query().Get("kind"))
	if err != nil {
		subscriptionError(w, http.StatusInternalServerError, "暂时无法读取订阅")
		return
	}
	runResponse(w, http.StatusOK, map[string][]SubscriptionView{"items": items})
}

func (h subscriptionHTTP) reveal(w http.ResponseWriter, r *http.Request) {
	id, ok := subscriptionRouteID(w, r)
	if !ok {
		return
	}
	var stored string
	err := h.db.QueryRowContext(r.Context(), `SELECT url FROM proxy_subscriptions WHERE id=?`, id).Scan(&stored)
	if errors.Is(err, sql.ErrNoRows) {
		subscriptionError(w, http.StatusNotFound, "订阅不存在")
		return
	}
	if err != nil {
		subscriptionError(w, http.StatusInternalServerError, "暂时无法读取订阅")
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	runResponse(w, http.StatusOK, map[string]any{"id": id, "url": OpenURL(h.dataDir, stored)})
}

func (h subscriptionHTTP) add(w http.ResponseWriter, r *http.Request) {
	req, ok := readSubscriptionEdit(w, r, true)
	if !ok {
		return
	}
	if req.Kind != "clash" {
		subscriptionError(w, http.StatusBadRequest, errSubscriptionKind.Error())
		return
	}
	ctx := r.Context()
	if h.beforeAdd != nil {
		if err := h.beforeAdd(ctx, req.Kind); err != nil {
			subscriptionError(w, http.StatusBadRequest, err.Error())
			return
		}
	}
	sealed, err := SealURL(h.dataDir, *req.URL)
	if err != nil {
		subscriptionError(w, http.StatusInternalServerError, "订阅链接无法安全保存")
		return
	}
	var label, stats any
	if req.Label != nil && *req.Label != "" {
		label, stats = *req.Label, `{"labelSource":"user"}`
	}
	var id int64
	err = h.gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		return conn.QueryRowContext(ctx, `INSERT INTO proxy_subscriptions(kind,url,label,created_at,last_stats,deprecated,admission_status,auto_refresh)
			VALUES ('clash',?,?,?,?,0,?,1) RETURNING id`, sealed, label, pyTimeText(time.Now()), stats, AdmissionCandidate).Scan(&id)
	})
	if err != nil {
		subscriptionError(w, http.StatusInternalServerError, "暂时无法保存订阅")
		return
	}
	response := map[string]any{"id": id, "kind": "clash", "url": maskSubscriptionURL(*req.URL), "label": label}
	if label != nil {
		response["label"] = safeSubscriptionText(label.(string))
	}
	if fields, ok := h.refreshFields(ctx, id); ok {
		copyRefreshFields(response, fields)
		response["cached"] = false
	} else {
		response["warning"] = "订阅已保存，本次下载未完成"
	}
	if h.afterAdd != nil {
		for key, value := range h.afterAdd(ctx, id) {
			response[key] = value
		}
	}
	runResponse(w, http.StatusOK, response)
}

func (h subscriptionHTTP) update(w http.ResponseWriter, r *http.Request) {
	id, ok := subscriptionRouteID(w, r)
	if !ok {
		return
	}
	req, ok := readSubscriptionEdit(w, r, false)
	if !ok {
		return
	}
	ctx := r.Context()
	var urlChanged, enabled bool
	var view SubscriptionView
	err := h.gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		var kind, sealed string
		var label, rawStats sql.NullString
		var automatic bool
		if err := conn.QueryRowContext(ctx, `SELECT kind,url,label,CAST(last_stats AS TEXT),COALESCE(auto_refresh,1)
			FROM proxy_subscriptions WHERE id=?`, id).Scan(&kind, &sealed, &label, &rawStats, &automatic); err != nil {
			return err
		}
		if kind != "clash" {
			return errSubscriptionKind
		}
		var stats map[string]any
		if jsonUnmarshalUseNumber([]byte(rawStats.String), &stats) != nil || stats == nil {
			stats = map[string]any{}
		}
		labelChanged := req.Label != nil && *req.Label != label.String
		if labelChanged {
			label = sql.NullString{String: *req.Label, Valid: *req.Label != ""}
			if label.Valid {
				stats["labelSource"] = "user"
			} else {
				delete(stats, "labelSource")
			}
		}
		if req.AutoRefresh != nil {
			enabled = !automatic && *req.AutoRefresh
			automatic = *req.AutoRefresh
		}
		urlChanged = req.URL != nil && !URLMatches(h.dataDir, sealed, *req.URL)
		if urlChanged {
			newURL, err := SealURL(h.dataDir, *req.URL)
			if err != nil {
				return err
			}
			if err := resetSubscriptionURL(ctx, conn, id, newURL); err != nil {
				return err
			}
			source := stats["labelSource"]
			clear(stats)
			switch source {
			case "user":
				stats["labelSource"] = source
			case "panel":
				label = sql.NullString{}
			}
		}
		if labelChanged || urlChanged {
			rawStats = sql.NullString{}
			if len(stats) > 0 {
				encoded, err := json.Marshal(stats)
				if err != nil {
					return err
				}
				rawStats = sql.NullString{String: string(encoded), Valid: true}
			}
		}
		if _, err := conn.ExecContext(ctx, `UPDATE proxy_subscriptions SET label=?,last_stats=?,auto_refresh=? WHERE id=?`, label, rawStats, automatic, id); err != nil {
			return err
		}
		var err error
		view, err = scanSubscriptionView(conn.QueryRowContext(ctx, subscriptionViewSQL+` WHERE id=?`, id), h.dataDir, time.Now())
		return err
	})
	switch {
	case errors.Is(err, sql.ErrNoRows):
		subscriptionError(w, http.StatusBadRequest, "订阅不存在")
	case errors.Is(err, errSubscriptionKind):
		subscriptionError(w, http.StatusBadRequest, errSubscriptionKind.Error())
	case err != nil:
		subscriptionError(w, http.StatusInternalServerError, "暂时无法更新订阅")
	default:
		if urlChanged && h.onChanged != nil {
			h.onChanged()
		}
		response := map[string]any{"id": id, "kind": view.Kind, "url": view.URL, "label": view.Label, "autoRefresh": view.AutoRefresh, "synced": false}
		if urlChanged || enabled {
			if fields, ok := h.refreshFields(ctx, id); ok {
				copyRefreshFields(response, fields)
				response["synced"] = true
			} else {
				response["warning"] = "订阅已更新，本次下载未完成"
			}
		}
		runResponse(w, http.StatusOK, response)
	}
}

func resetSubscriptionURL(ctx context.Context, conn *sql.Conn, id int64, sealed string) error {
	if err := LockSubscriptionMutation(ctx, conn, id); err != nil {
		return err
	}
	rows, err := conn.QueryContext(ctx, `UPDATE proxy_nodes SET subscription_id=NULL WHERE subscription_id=? RETURNING node_id`, id)
	if err != nil {
		return err
	}
	defer rows.Close()
	nodes := map[string]bool{}
	for rows.Next() {
		var node string
		if err := rows.Scan(&node); err != nil {
			return err
		}
		nodes[node] = true
	}
	if err := rows.Err(); err != nil {
		return err
	}
	if _, err := SettleNodesAfterSourceChange(ctx, conn, nodes, time.Now()); err != nil {
		return err
	}
	_, err = conn.ExecContext(ctx, `UPDATE proxy_subscriptions SET url=?,admission_status=?,
		last_imported_at=NULL,last_fetch_at=NULL,last_fetch_status=NULL,last_success_at=NULL,last_error=NULL,
		cache_url_identity=NULL,deprecated=0,deprecated_at=NULL,deprecated_reason=NULL WHERE id=?`, sealed, AdmissionCandidate, id)
	return err
}

func (h subscriptionHTTP) refreshRoute(trafficOnly bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		id, ok := subscriptionRouteID(w, r)
		if !ok {
			return
		}
		ctx := r.Context()
		var kind string
		err := h.db.QueryRowContext(ctx, `SELECT kind FROM proxy_subscriptions WHERE id=?`, id).Scan(&kind)
		switch {
		case errors.Is(err, sql.ErrNoRows):
			subscriptionError(w, http.StatusBadRequest, "订阅不存在")
			return
		case err != nil:
			subscriptionError(w, http.StatusInternalServerError, "暂时无法读取订阅")
			return
		case kind != "clash":
			subscriptionError(w, http.StatusBadRequest, errSubscriptionKind.Error())
			return
		}
		fields, ok := h.refreshFields(ctx, id)
		if !ok {
			subscriptionError(w, http.StatusBadGateway, "本次订阅下载未完成")
			return
		}
		if trafficOnly {
			if fields["traffic"] == nil {
				subscriptionError(w, http.StatusBadGateway, "订阅未返回新的流量信息")
				return
			}
			fields = map[string]any{"id": id, "traffic": fields["traffic"]}
		}
		runResponse(w, http.StatusOK, fields)
	}
}

func (h subscriptionHTTP) refreshFields(ctx context.Context, id int64) (map[string]any, bool) {
	if h.refresh == nil {
		return nil, false
	}
	result, err := h.refresh(ctx, id)
	if err != nil || result == nil || result.Failures[id] != "" {
		return nil, false
	}
	snapshot := result.Snapshots[id]
	count, ok := result.NodeCount[id]
	if !ok || snapshot == nil {
		return nil, false
	}
	view, err := scanSubscriptionView(h.db.QueryRowContext(ctx, subscriptionViewSQL+` WHERE id=?`, id), h.dataDir, time.Now())
	if err != nil {
		return nil, false
	}
	fields := map[string]any{"id": id, "label": view.Label, "nodes": count, "traffic": nil, "alive": 0, "total": 0, "usedCache": false, "restarted": false, "strategySwitched": result.StrategySwitched}
	if snapshot.Userinfo != "" {
		fields["traffic"] = safeSubscriptionText(snapshot.Userinfo)
	}
	if stats := view.LastStats; stats != nil {
		if stats.Alive != nil {
			fields["alive"] = *stats.Alive
		}
		if stats.Total != nil {
			fields["total"] = *stats.Total
		}
	}
	return fields, true
}

func copyRefreshFields(response, fields map[string]any) {
	for _, key := range []string{"label", "nodes", "traffic", "alive", "total", "restarted", "strategySwitched"} {
		if value, exists := fields[key]; exists {
			response[key] = value
		}
	}
}

// deleteRoute DELETE /subscriptions/{id}：clash 订阅先退出生产池并随行删
// 快照行，再删订阅行；事务外回收快照目录与内核缓存产物，最后无出口回退
// 直连（Python delete_subscription 同构）。
func (h subscriptionHTTP) deleteRoute(w http.ResponseWriter, r *http.Request) {
	id, ok := subscriptionRouteID(w, r)
	if !ok {
		return
	}
	ctx := r.Context()
	var kind string
	err := h.db.QueryRowContext(ctx, `SELECT kind FROM proxy_subscriptions WHERE id=?`, id).Scan(&kind)
	if errors.Is(err, sql.ErrNoRows) {
		subscriptionError(w, http.StatusNotFound, "订阅不存在")
		return
	}
	if err != nil {
		subscriptionError(w, http.StatusInternalServerError, "暂时无法读取订阅")
		return
	}
	err = h.gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		if kind == "clash" {
			if _, err := ExitFromProduction(ctx, conn, id); err != nil {
				var promotion *PromotionError
				if errors.As(err, &promotion) && promotion.Detail != "" &&
					strings.Contains(promotion.Detail, "订阅不存在") {
					return errSubscriptionGone
				}
				return err
			}
			if _, err := conn.ExecContext(ctx,
				`DELETE FROM subscription_snapshots WHERE subscription_id=?`, id); err != nil {
				return err
			}
		}
		result, err := conn.ExecContext(ctx, `DELETE FROM proxy_subscriptions WHERE id=?`, id)
		if err != nil {
			return err
		}
		if affected, err := result.RowsAffected(); err == nil && affected == 0 {
			return errSubscriptionGone
		}
		return nil
	})
	switch {
	case errors.Is(err, errSubscriptionGone):
		subscriptionError(w, http.StatusNotFound, "订阅不存在")
		return
	case err != nil:
		subscriptionError(w, http.StatusInternalServerError, "暂时无法删除订阅")
		return
	}
	// 行已删，快照原始字节按订阅目录整体回收（内容寻址，行没了字节即孤儿）
	_ = os.RemoveAll(filepath.Join(snapshotDir(h.dataDir), strconv.FormatInt(id, 10)))
	if h.afterDelete != nil {
		h.afterDelete(ctx, id, kind)
	}
	if h.revertToDirect != nil {
		h.revertToDirect(ctx)
	}
	runResponse(w, http.StatusOK, map[string]any{"removed": true})
}

// errSubscriptionGone 删除窗口内订阅行已消失（并发删除守卫）。
var errSubscriptionGone = errors.New("订阅不存在")

// importRoute POST /subscriptions/{id}/import：拉取明文代理订阅并导入节点池
// （Python import_plain_subscription 同构；非 plain 一律 400 逐字文案）。
func (h subscriptionHTTP) importRoute(w http.ResponseWriter, r *http.Request) {
	id, ok := subscriptionRouteID(w, r)
	if !ok {
		return
	}
	ctx := r.Context()
	var kind, sealed string
	err := h.db.QueryRowContext(ctx, `SELECT kind,url FROM proxy_subscriptions WHERE id=?`, id).Scan(&kind, &sealed)
	if errors.Is(err, sql.ErrNoRows) {
		subscriptionError(w, http.StatusBadRequest, "订阅不存在")
		return
	}
	if err != nil {
		subscriptionError(w, http.StatusInternalServerError, "暂时无法读取订阅")
		return
	}
	if kind != "plain" {
		subscriptionError(w, http.StatusBadRequest, "该订阅不是明文代理订阅（kind=plain）")
		return
	}
	subURL := OpenURL(h.dataDir, sealed)
	if subURL == "" {
		subscriptionError(w, http.StatusBadRequest, UnreadableMessage)
		return
	}
	stats, importErr := h.importPlain(ctx, id, subURL)
	if importErr != nil {
		subscriptionError(w, http.StatusBadRequest, importErr.Error())
		return
	}
	runResponse(w, http.StatusOK, stats)
}

// parsePlainProxyLine 明文代理行解析（Python parse_plain_proxy_line 同构，
// 四格式：scheme://user:pass@host:port | host:port | host:port:user:pass |
// user:pass@host:port；注释行与空行回 nil）。
func parsePlainProxyLine(line string) map[string]any {
	raw := strings.TrimSpace(line)
	if raw == "" || strings.HasPrefix(raw, "#") {
		return nil
	}
	if strings.Contains(raw, "://") {
		parsed, err := parseProxyURL(raw)
		if err != nil {
			return nil
		}
		return proxyLineMap(parsed.scheme, parsed.host, parsed.port, parsed.username, parsed.password)
	}
	if strings.Contains(raw, "@") {
		auth, addr, _ := strings.Cut(raw, "@")
		idx := strings.LastIndex(auth, ":")
		user, pwd := "", ""
		if idx >= 0 {
			user, pwd = auth[:idx], auth[idx+1:]
		} else {
			user = auth
		}
		host, port, _ := strings.Cut(addr, ":")
		if host != "" && isDigits(port) {
			var username, password *string
			if user != "" {
				username = &user
			}
			if pwd != "" {
				password = &pwd
			}
			n, _ := strconv.Atoi(port)
			return proxyLineMap("http", host, n, username, password)
		}
		return nil
	}
	parts := strings.Split(raw, ":")
	if len(parts) == 2 && isDigits(parts[1]) {
		n, _ := strconv.Atoi(parts[1])
		return proxyLineMap("http", parts[0], n, nil, nil)
	}
	if len(parts) == 4 && isDigits(parts[1]) {
		n, _ := strconv.Atoi(parts[1])
		username, password := parts[2], parts[3]
		return proxyLineMap("http", parts[0], n, &username, &password)
	}
	return nil
}

func proxyLineMap(scheme, host string, port int, username, password *string) map[string]any {
	return map[string]any{"scheme": scheme, "host": host, "port": port,
		"username": username, "password": password}
}

func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

// parsePlainProxyContent 响应原文 → 明文代理行列表（FetchOptions.ResponseParser
// 装配形态；解析失败的行跳过）。
func parsePlainProxyContent(raw []byte, _ string) ([]map[string]any, error) {
	parsed := []map[string]any{}
	for _, line := range strings.Split(string(raw), "\n") {
		if item := parsePlainProxyLine(line); item != nil {
			parsed = append(parsed, item)
		}
	}
	return parsed, nil
}

// importPlain 拉取与入库主链：抓取 → 快照落账 → 凭据密封逐行导入（按
// host:port:解密用户名去重）→ 新行体检 → mark_imported → 走线事件。
func (h subscriptionHTTP) importPlain(ctx context.Context, id int64, subURL string) (map[string]any, error) {
	now := time.Now()
	channels := BuildChannels(h.kernelProxyOf(ctx), h.poolChannelsOf(ctx))
	opts := FetchOptions{ResponseParser: parsePlainProxyContent}
	fetched, fetchErr := FetchSubscription(ctx, subURL, channels, opts)
	if fetchErr != nil {
		attempts, durationMS := fetchEvidence(fetchErr)
		now = time.Now()
		_ = h.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
			if err := PersistFetchFailure(ctx, conn, FailureRecord{
				SubscriptionID: id, DataDir: h.dataDir, URL: subURL,
				Attempts: attempts, Error: errMessage(fetchErr), DurationMS: durationMS, Now: now,
			}); err != nil {
				return err
			}
			_, err := conn.ExecContext(ctx,
				`UPDATE proxy_subscriptions SET last_fetch_at=?, last_fetch_status='FAILED', last_error=? WHERE id=?`,
				pyTimeText(now), truncateRunes(errMessage(fetchErr), 500), id)
			return err
		})
		h.recordEvent(ctx, "import", fmt.Sprintf("plain:%d", id), lastHTTPStatus(attempts), durationMS, errMessage(fetchErr))
		return nil, errors.New(errMessage(fetchErr))
	}
	parsed := fetched.ResolvedNodes
	if len(parsed) == 0 {
		return nil, errors.New("订阅内容没有可解析的代理行")
	}
	snapshot := BuildSnapshot(id, subURL, fetched, parsed, now)
	stats := map[string]any{"fetched": len(parsed), "added": 0, "skipped": 0}
	var newIDs []int64
	err := h.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		if err := LockSubscriptionMutation(ctx, conn, id); err != nil {
			return err
		}
		if _, err := PersistSnapshot(ctx, conn, h.dataDir, snapshot); err != nil {
			return err
		}
		if _, err := conn.ExecContext(ctx,
			`UPDATE proxy_subscriptions SET cache_url_identity=?, last_fetch_at=?, last_fetch_status='OK', last_error=NULL WHERE id=?`,
			SubscriptionURLIdentity(subURL), pyTimeText(now), id); err != nil {
			return err
		}
		added, skipped, fresh, err := ingestPlainNodes(ctx, conn, h.dataDir, parsed)
		if err != nil {
			return err
		}
		stats["added"], stats["skipped"] = added, skipped
		newIDs = fresh
		return nil
	})
	if err != nil {
		return nil, err
	}
	// 入库即体检：只测本次新增行（存量沿用既有 status，重测留给 6h 体检）；
	// 失败保留数据，连败计数自然处置。逐条串行（登记差异：Python 并发 gather）。
	if len(newIDs) > 0 {
		checker := &ManualHealth{DB: h.db, Gate: h.gate, DataDir: h.dataDir}
		alive := 0
		for _, rowID := range newIDs {
			items, err := checker.Check(ctx, rowID, false)
			if err == nil && len(items) > 0 {
				var view map[string]any
				if json.Unmarshal(items[0], &view) == nil && view["status"] == "ok" {
					alive++
				}
			}
		}
		stats["checked"] = len(newIDs)
		stats["alive"] = alive
		h.recordEvent(ctx, "import", fmt.Sprintf("plain:%d", id), lastHTTPStatus(fetched.Attempts), fetched.DurationMS, nil)
	} else {
		h.recordEvent(ctx, "import", fmt.Sprintf("plain:%d", id), lastHTTPStatus(fetched.Attempts), fetched.DurationMS, nil)
	}
	if err := h.markImported(ctx, id, stats); err != nil {
		return nil, err
	}
	if alive(stats) > 0 && h.autoswitchOnSubscription(ctx, alive(stats)) {
		stats["strategySwitched"] = true
	}
	return stats, nil
}

func alive(stats map[string]any) int {
	if value, ok := stats["alive"].(int); ok {
		return value
	}
	return 0
}

// ingestPlainNodes 逐行去重导入（去重键 = host + port + 解密后用户名）；
// 新增行密封凭据落库。返回（added, skipped, 新增行 id 列表）。
func ingestPlainNodes(ctx context.Context, conn *sql.Conn, dataDir string, parsed []map[string]any,
) (int, int, []int64, error) {
	existing := map[string]bool{}
	rows, err := conn.QueryContext(ctx, `SELECT host, port, username FROM proxies`)
	if err != nil {
		return 0, 0, nil, err
	}
	for rows.Next() {
		var host string
		var port int64
		var username sql.NullString
		if err := rows.Scan(&host, &port, &username); err != nil {
			rows.Close()
			return 0, 0, nil, err
		}
		existing[nodeKey(host, port, openProxyCredential(dataDir, username.String))] = true
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return 0, 0, nil, err
	}
	rows.Close()
	added, skipped := 0, 0
	var fresh []int64
	for _, item := range parsed {
		host, _ := item["host"].(string)
		port, _ := item["port"].(int)
		username, _ := item["username"].(*string)
		password, _ := item["password"].(*string)
		scheme, _ := item["scheme"].(string)
		if scheme == "" {
			scheme = "http"
		}
		key := nodeKey(host, int64(port), stringOrDeref(username))
		if existing[key] {
			skipped++
			continue
		}
		existing[key] = true
		var label any
		if username != nil && *username != "" {
			label = fmt.Sprintf("%s:%d(%s)", host, port, *username)
		}
		sealedUser, sealedPass := "", ""
		if username != nil && *username != "" {
			sealed, err := sealProxyCredential(dataDir, *username)
			if err != nil {
				return added, skipped, fresh, err
			}
			sealedUser = sealed
		}
		if password != nil && *password != "" {
			sealed, err := sealProxyCredential(dataDir, *password)
			if err != nil {
				return added, skipped, fresh, err
			}
			sealedPass = sealed
		}
		result, err := conn.ExecContext(ctx, `INSERT INTO proxies(label,scheme,host,port,username,password,enabled,status,created_at)
			VALUES(?,?,?,?,?,?,1,'unknown',?)`, label, scheme, host, port,
			nullString(sealedUser), nullString(sealedPass), pyTimeText(time.Now()))
		if err != nil {
			return added, skipped, fresh, err
		}
		newID, err := result.LastInsertId()
		if err == nil {
			fresh = append(fresh, newID)
		}
		added++
	}
	return added, skipped, fresh, nil
}

func nodeKey(host string, port int64, username string) string {
	return fmt.Sprintf("%s|%d|%s", host, port, username)
}

func stringOrDeref(v *string) string {
	if v == nil {
		return ""
	}
	return *v
}

// markImported last_imported_at + last_stats 记账（Python mark_imported 同径）。
func (h subscriptionHTTP) markImported(ctx context.Context, id int64, stats map[string]any) error {
	encoded, err := json.Marshal(stats)
	if err != nil {
		return err
	}
	return h.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		_, err := conn.ExecContext(ctx,
			`UPDATE proxy_subscriptions SET last_imported_at=?, last_stats=? WHERE id=?`,
			pyTimeText(time.Now()), string(encoded), id)
		return err
	})
}

// recordEvent 走线事件记账（失败只留日志不拖垮导入链，Python record_event 同径）。
func (h subscriptionHTTP) recordEvent(ctx context.Context, kind, target string, statusCode any, durationMS int, errMsg any) {
	_ = h.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		_, err := conn.ExecContext(ctx,
			`INSERT INTO proxy_events(ts,kind,target,proxy_label,status_code,duration_ms,error) VALUES(?,?,?,?,?,?,?)`,
			pyTimeText(time.Now()), kind, target, target, statusCode, durationMS, errMsg)
		return err
	})
}

// autoswitchOnSubscription 导入有效订阅后自动切代理优先（Python
// maybe_autoswitch_on_subscription 同构：直连 + 未显式选过 + 有存活节点）。
func (h subscriptionHTTP) autoswitchOnSubscription(ctx context.Context, nodes int) bool {
	if nodes <= 0 {
		return false
	}
	switched := false
	_ = h.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		var strategy string
		var explicit bool
		if err := conn.QueryRowContext(ctx, `SELECT
			COALESCE((SELECT json_extract(value_json,'$') FROM app_settings WHERE key='proxy.strategy'),'direct_only'),
			COALESCE((SELECT json_extract(value_json,'$')=1 FROM app_settings WHERE key='proxy.strategy_explicit'),0)`).
			Scan(&strategy, &explicit); err != nil {
			return err
		}
		if explicit || strategy != "direct_only" {
			return nil
		}
		if err := settings.WriteValue(ctx, conn, "proxy.strategy", "proxy_first"); err != nil {
			return err
		}
		switched = true
		return nil
	})
	return switched
}

func (h subscriptionHTTP) kernelProxyOf(ctx context.Context) string {
	if h.kernelProxy == nil {
		return ""
	}
	return h.kernelProxy(ctx)
}

func (h subscriptionHTTP) poolChannelsOf(ctx context.Context) []Channel {
	if h.channels == nil {
		return nil
	}
	return h.channels(ctx)
}

// fetchEvidence 失败错误 → 通道尝试留痕（对齐 Python getattr(exc, "attempts", [])）。
func fetchEvidence(err error) ([]FetchAttempt, int) {
	var ffe *FetchFailedError
	var ufe *UnsupportedFormatError
	var ene *EmptyNodeSetError
	var iee *InvalidEncodingError
	switch {
	case errors.As(err, &ffe):
		return ffe.Attempts, ffe.DurationMS
	case errors.As(err, &ufe):
		return ufe.Attempts, ufe.DurationMS
	case errors.As(err, &ene):
		return ene.Attempts, ene.DurationMS
	case errors.As(err, &iee):
		return iee.Attempts, iee.DurationMS
	}
	return nil, 0
}

// lastHTTPStatus 逆序首个 HTTP 状态码（Python persist 失败行同径）。
func lastHTTPStatus(attempts []FetchAttempt) any {
	for i := len(attempts) - 1; i >= 0; i-- {
		if attempts[i].HTTPStatus != nil {
			return *attempts[i].HTTPStatus
		}
	}
	return nil
}
